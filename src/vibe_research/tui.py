"""The Textual TUI for vibe-research.

Left pane: live activity log. Right pane: the rendered Markdown report.
A status line under the input shows the current stage, a progress bar, elapsed
time, findings completed, and how many sources have been gathered.

Type a topic and press Enter, or pass one on the command line.

Keys:  Ctrl+N new topic - Ctrl+L clear log - Ctrl+S copy report - F2 light/dark - Ctrl+Q quit
"""

from __future__ import annotations

import sys
import time

from rich.markup import escape as _esc
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Footer, Header, Input, Markdown, RichLog, Static

from .backends import estimate_cost, get_backend, resolve_models
from .config import Config
from .pipeline import run_kwargs_from_config, run_pipeline
from .reports import save_report

_STAGE_LABELS = {
    "plan": "1) Planning",
    "research": "2) Researching",
    "verify": "3) Fact-checking",
    "critique": "4) Editor review",
    "write": "5) Writing",
    "humanize": "6) Humanizing",
}

# Coarse progress (%) reached when each stage *begins*. Research is the variable
# part, so it interpolates between its start and the verify boundary as findings
# land. Progress is kept monotonic (never jumps backwards) so the self-refining
# loop's extra research rounds don't rewind the bar.
_STAGE_PROGRESS = {
    "plan": 5, "research": 8, "verify": 68, "critique": 80, "write": 88, "humanize": 95,
}
_RESEARCH_SPAN = _STAGE_PROGRESS["verify"] - _STAGE_PROGRESS["research"]  # 8 -> 68

_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_IDLE_HINT = "Enter a research topic and press Enter…"


def _bar(pct: float, width: int = 22) -> str:
    """A markup progress bar drawn with block characters (no widget-layout risk)."""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(width * pct / 100.0))
    return f"[green]{'█' * filled}[/green][dim]{'░' * (width - filled)}[/dim]"


class VibeResearchApp(App):
    CSS = """
    #topic  { margin: 1 1 0 1; }
    #status { height: 1; padding: 0 2; color: $text-muted; background: $panel; }
    #panes  { height: 1fr; padding: 0 1; }
    #log    {
        width: 42%; border: round $accent; padding: 0 1; margin: 0 1 0 0;
        border-title-color: $accent; border-title-align: center; scrollbar-size: 1 1;
    }
    #report {
        width: 58%; border: round $success; padding: 0 1;
        border-title-color: $success; border-title-align: center;
    }
    #report Markdown { padding: 0; }
    """
    TITLE = "vibe-research"
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("ctrl+c", "quit", "Quit", show=False),
        Binding("ctrl+n", "new_topic", "New topic"),
        Binding("ctrl+l", "clear_log", "Clear log"),
        Binding("ctrl+s", "copy_report", "Copy report"),
        Binding("ctrl+p", "export_pdf", "Export PDF"),
        Binding("ctrl+e", "export_all", "Export all"),
        Binding("ctrl+o", "open_report", "Open"),
        Binding("f2", "toggle_dark", "Light/Dark"),
    ]

    def __init__(self, cfg: Config, topic: str | None = None) -> None:
        super().__init__()
        self.cfg = cfg
        self.start_topic = topic
        self._busy = False
        # run state (drives the status line)
        self._stage = ""
        self._pct = 0.0
        self._total_q = 0
        self._done_q = 0
        self._sources = 0
        self._t0 = 0.0
        self._tick = 0
        self._timer = None
        self._report_text = ""
        self._final_path = ""
        self._confidence = None
        self._result = None
        self._backend = None
        self._last_topic = topic or ""

    # ---------------------------------------------------------------- layout

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder=_IDLE_HINT, id="topic")
        yield Static("", id="status")
        with Horizontal(id="panes"):
            yield RichLog(id="log", highlight=True, markup=True, wrap=True)
            with VerticalScroll(id="report"):
                yield Markdown(
                    "# Report will appear here\n\n"
                    "Type a topic above and press **Enter** to begin.\n\n"
                    "A crew of agents will plan, research, fact-check (by debate), "
                    "and refine — then write a cited report here, with sources ranked "
                    "by credibility.\n\n"
                    "_Ctrl+E export all formats · Ctrl+P PDF · Ctrl+O open · Ctrl+S copy._"
                )
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"mode: {self.cfg.mode}"
        self.query_one("#log", RichLog).border_title = "Activity"
        self.query_one("#report", VerticalScroll).border_title = "Report"
        self._refresh_status()
        if self.start_topic:
            self.query_one("#topic", Input).value = self.start_topic
            self.call_after_refresh(self.start_research, self.start_topic)

    # ------------------------------------------------------------- utilities

    def _log(self, message: str) -> None:
        self.query_one("#log", RichLog).write(message)

    def _report(self):
        return self.query_one("#report Markdown", Markdown)

    def _elapsed(self) -> str:
        secs = int(time.monotonic() - self._t0) if self._t0 else 0
        return f"{secs // 60:d}:{secs % 60:02d}"

    def _usage_str(self) -> str:
        """Live calls + token count from the running backend (empty if none yet)."""
        if not self._backend:
            return ""
        try:
            u = self._backend.usage()
        except Exception:
            return ""
        parts = []
        if u.get("calls"):
            parts.append(f"{u['calls']} calls")
        tok = int(u.get("input_tokens", 0) or 0) + int(u.get("output_tokens", 0) or 0)
        if tok:
            parts.append(f"{tok / 1000:.1f}k tok" if tok >= 1000 else f"{tok} tok")
        return "  ·  ".join(parts)

    def _refresh_status(self) -> None:
        status = self.query_one("#status", Static)
        if self._busy:
            spin = _SPINNER[self._tick % len(_SPINNER)]
            total = self._total_q or "?"
            counts = f"findings {self._done_q}/{total}  ·  {self._sources} src"
            usage = self._usage_str()
            usage_seg = f"  ·  {usage}" if usage else ""
            status.update(
                f"{spin} [b]{self._stage or 'working'}[/b]  {_bar(self._pct)} "
                f"{int(self._pct):>3d}%  ·  {self._elapsed()}  ·  {counts}{usage_seg}  ·  {self.cfg.mode}"
            )
        elif self._final_path:
            conf = f"  ·  [b]{self._confidence:.0%} confidence[/b]" if self._confidence is not None else ""
            status.update(
                f"[green]✔ done[/green]  ·  {self._elapsed()}  ·  {self._done_q} findings  ·  "
                f"{self._sources} src{conf}  ·  saved [u]{_esc(self._final_path)}[/u]"
            )
        else:
            status.update(
                f"[dim]idle  ·  mode {self.cfg.mode}  ·  press Ctrl+N to enter a topic[/dim]"
            )

    def _on_tick(self) -> None:
        self._tick += 1
        self._refresh_status()

    def _start_timer(self) -> None:
        if self._timer is None:
            self._timer = self.set_interval(1 / 8, self._on_tick)

    def _stop_timer(self) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer = None

    # --------------------------------------------------------------- actions

    def action_new_topic(self) -> None:
        inp = self.query_one("#topic", Input)
        if inp.disabled:
            self._log("[yellow]Still researching — please wait for the current run to finish.[/yellow]")
            return
        inp.value = ""
        inp.focus()

    def action_clear_log(self) -> None:
        self.query_one("#log", RichLog).clear()

    def action_copy_report(self) -> None:
        if not self._report_text:
            self._log("[yellow]No report to copy yet.[/yellow]")
            return
        try:
            self.copy_to_clipboard(self._report_text)
            self._log("[green]Report copied to clipboard.[/green]")
        except Exception as exc:  # pragma: no cover - clipboard is environment-dependent
            self._log(f"[red]Copy failed: {_esc(str(exc))}[/red]")

    def action_export_pdf(self) -> None:
        if not self._report_text:
            self._log("[yellow]No report to export yet — finish a run first.[/yellow]")
            return
        try:
            from .export import markdown_to_pdf, pdf_path_for
        except Exception as exc:  # pragma: no cover - import guard
            self._log(f"[red]PDF export unavailable: {_esc(str(exc))}[/red]")
            return
        try:
            if self._final_path:
                out = pdf_path_for(self._final_path)
            else:
                out = self.cfg.resolved_reports_dir() / "report.pdf"
            path = markdown_to_pdf(
                self._report_text, out, title=self._last_topic or "vibe-research report",
                base_dir=out.parent,
            )
            self._log(f"[green]✔ PDF exported: {_esc(str(path))}[/green]")
        except Exception as exc:
            self._log(f"[red]✗ PDF export failed: {_esc(str(exc))}[/red]")

    def action_export_all(self) -> None:
        """Export the current report to every available format on demand."""
        if not self._report_text:
            self._log("[yellow]No report to export yet — finish a run first.[/yellow]")
            return
        from pathlib import Path

        if self._final_path:
            base = Path(self._final_path)
            md = base.read_text(encoding="utf-8")
        else:
            base = self.cfg.resolved_reports_dir() / "report.md"
            base.parent.mkdir(parents=True, exist_ok=True)
            md = self._report_text
        title = self._last_topic or "vibe-research report"
        made: list = []

        try:
            from .export import (
                docx_available, docx_path_for, html_path_for, markdown_to_docx,
                markdown_to_html_file, markdown_to_pdf, pdf_available, pdf_path_for,
            )

            made.append(markdown_to_html_file(md, html_path_for(base), title=title))
            if pdf_available():
                made.append(markdown_to_pdf(md, pdf_path_for(base), title=title, base_dir=base.parent))
            if docx_available():
                made.append(markdown_to_docx(md, docx_path_for(base), title=title, base_dir=base.parent))
        except Exception as exc:
            self._log(f"[red]Export error: {_esc(str(exc))}[/red]")
        if self._result:
            try:
                from .reports import save_json

                made.append(save_json(base, self._result))
            except Exception:
                pass

        for out in made:
            self._log(f"[green]✔ exported {_esc(str(out))}[/green]")
        if not made:
            self._log("[yellow]Nothing exported (install fpdf2 / python-docx for PDF/DOCX).[/yellow]")

    def _open_path(self, path) -> None:
        import subprocess

        try:
            if sys.platform == "win32":
                import os

                os.startfile(str(path))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except Exception as exc:
            self._log(f"[red]Open failed: {_esc(str(exc))}[/red]")

    def action_open_report(self) -> None:
        if self._final_path:
            self._open_path(self._final_path)
        else:
            self._log("[yellow]No saved report to open yet.[/yellow]")

    def _save_extras(self, path, report: str, topic: str) -> None:
        """Write the JSON sidecar and/or HTML page if the config asks for them."""
        if self.cfg.export_json and self._result:
            try:
                from .reports import save_json

                jp = save_json(path, self._result)
                self._log(f"[green]✔ JSON: {_esc(str(jp))}[/green]")
            except Exception as exc:
                self._log(f"[red]JSON export failed: {_esc(str(exc))}[/red]")
        if self.cfg.export_html:
            try:
                from .export import html_path_for, markdown_to_html_file

                md = path.read_text(encoding="utf-8")
                hp = markdown_to_html_file(md, html_path_for(path), title=topic)
                self._log(f"[green]✔ HTML: {_esc(str(hp))}[/green]")
            except Exception as exc:
                self._log(f"[red]HTML export failed: {_esc(str(exc))}[/red]")
        if self.cfg.export_docx:
            try:
                from .export import docx_path_for, markdown_to_docx

                md = path.read_text(encoding="utf-8")
                dp = markdown_to_docx(md, docx_path_for(path), title=topic, base_dir=path.parent)
                self._log(f"[green]✔ DOCX: {_esc(str(dp))}[/green]")
            except Exception as exc:
                self._log(f"[red]DOCX export failed: {_esc(str(exc))}[/red]")

    def action_toggle_dark(self) -> None:
        # Works across Textual versions: newer uses a theme system, older uses `dark`.
        try:
            self.theme = "textual-light" if self.theme == "textual-dark" else "textual-dark"
        except Exception:
            try:
                self.dark = not self.dark  # type: ignore[attr-defined]
            except Exception:
                pass

    # ----------------------------------------------------------------- input

    def on_input_submitted(self, event: Input.Submitted) -> None:
        topic = event.value.strip()
        if topic:
            self.start_research(topic)

    def start_research(self, topic: str) -> None:
        if self._busy:
            self._log("[yellow]Already researching — please wait for the current run to finish.[/yellow]")
            return
        self.run_pipeline_worker(topic)

    # ------------------------------------------------------------ the worker

    @work(exclusive=True)
    async def run_pipeline_worker(self, topic: str) -> None:
        self._busy = True
        self._stage = "starting"
        self._pct = 2.0
        self._total_q = self._done_q = self._sources = 0
        self._report_text = ""
        self._final_path = ""
        self._confidence = None
        self._result = None
        self._backend = None
        self._last_topic = topic
        self._t0 = time.monotonic()
        self._tick = 0

        inp = self.query_one("#topic", Input)
        inp.disabled = True
        self.query_one("#log", RichLog).clear()
        self._report().update("# Researching…\n\nSee the activity log on the left for live progress.")
        self._start_timer()
        self._refresh_status()

        def on_event(kind: str, data: dict) -> None:
            if kind == "start":
                self._log(f"[b]Topic:[/b] {_esc(data['topic'])}")
                debate = f"{self.cfg.verifier_votes}-vote debate" if self.cfg.enable_debate else "single check"
                mem = "memory on" if self.cfg.enable_memory else "memory off"
                hum = "humanized" if self.cfg.humanize else "raw voice"
                self._log(
                    f"[dim]mode {self.cfg.mode} · {self.cfg.subquestions} sub-questions · "
                    f"{self.cfg.max_parallel} parallel · ≤{self.cfg.max_iterations} rounds · "
                    f"{debate} · {mem} · {hum}[/dim]"
                )
            elif kind == "stage":
                stage = data["stage"]
                self._stage = _STAGE_LABELS.get(stage, stage)
                if stage in _STAGE_PROGRESS:
                    self._pct = max(self._pct, float(_STAGE_PROGRESS[stage]))
                self._log(f"[cyan]{self._stage} — {_esc(data['msg'])}…[/cyan]")
            elif kind == "memory":
                if data.get("recalled"):
                    topics = ", ".join(data.get("topics", []))
                    self._log(f"   [blue]🧠 recalled {data['recalled']} related past run(s):[/blue] "
                              f"[dim]{_esc(topics[:70])}[/dim]")
                elif data.get("saved"):
                    self._log("   [dim]🧠 saved to long-term memory[/dim]")
            elif kind == "plan":
                self._total_q = len(data["questions"])
                for question in data["questions"]:
                    self._log(f"   [dim]•[/dim] {_esc(question)}")
            elif kind == "finding":
                self._done_q += 1
                self._sources += int(data.get("n_sources", 0))
                if self._total_q:
                    frac = min(1.0, self._done_q / self._total_q)
                    self._pct = max(self._pct, _STAGE_PROGRESS["research"] + _RESEARCH_SPAN * frac)
                q = data["question"]
                tail = "…" if len(q) > 60 else ""
                self._log(
                    f"   [green]✓[/green] {_esc(q[:60])}{tail}  [dim]({data['n_sources']} sources)[/dim]"
                )
            elif kind == "debate":
                self._log(
                    f"   [magenta]⚖[/magenta]  {_esc(data['question'][:55])}  "
                    f"[dim]→ confidence {data['confidence']:.0%} ({data['votes']} votes)[/dim]"
                )
            elif kind == "verify":
                # Surface the skeptic's notes — they're the point.
                self._log("")
                self._log("[b magenta]Fact-check debate[/b magenta]")
                review = (data.get("review") or "").strip()
                for line in (review.splitlines() or ["(no notes returned)"]):
                    self._log(f"  [dim]│[/dim] {_esc(line)}")
            elif kind == "critique":
                verdict = "[green]approved[/green]" if data["approved"] else "[yellow]needs more[/yellow]"
                self._log(
                    f"   [b]✎ editor:[/b] {verdict}  [dim](quality {data['quality']:.0%}, "
                    f"overall confidence {data['confidence']:.0%})[/dim]"
                )
                for miss in data.get("missing", [])[:5]:
                    self._log(f"      [yellow]gap →[/yellow] {_esc(miss[:80])}")
            elif kind == "iteration":
                self._log(
                    f"[b cyan]↻ round {data['round']}/{data['max']}[/b cyan] "
                    f"[dim]— researching {len(data.get('gaps', []))} gap thread(s)[/dim]"
                )
            elif kind == "done":
                self._pct = 100.0
                self._confidence = data.get("confidence")
                self._result = data.get("result")
            self._refresh_status()

        backend = None
        try:
            backend = get_backend(self.cfg.mode)
            backend.configure(
                max_retries=self.cfg.max_retries,
                call_timeout=self.cfg.call_timeout,
                max_concurrency=self.cfg.max_concurrency,
            )
            if self.cfg.debug:
                try:
                    reports_dir = self.cfg.resolved_reports_dir()
                    reports_dir.mkdir(parents=True, exist_ok=True)
                    dbg = reports_dir / f"vibe-debug-{int(time.time())}.jsonl"
                    backend.configure(debug_path=dbg)
                    self._log(f"[dim]🐞 debug trace: {_esc(str(dbg))}[/dim]")
                except Exception:
                    pass
            # Resolve models for the chosen provider and surface them live.
            self.cfg.planner_model, self.cfg.worker_model = resolve_models(
                backend.name, self.cfg.planner_model, self.cfg.worker_model
            )
            self._backend = backend
            self.sub_title = (
                f"{backend.name} · {self.cfg.planner_model} + {self.cfg.worker_model}"
            )
        except Exception as exc:
            self._log(f"[red]✗ {_esc(str(exc))}[/red]")
            self._report().update(
                f"# Setup needed\n\n```\n{exc}\n```\n\nRun `vibe-research doctor` for a full environment check."
            )
            self._stage = "setup needed"
            return

        try:
            report = await run_pipeline(
                backend,
                topic,
                on_event=on_event,
                **run_kwargs_from_config(self.cfg),
            )
            self._report_text = report
            self._report().update(report)
            reports_dir = self.cfg.resolved_reports_dir()
            path = save_report(reports_dir, topic, report, meta=self._result)
            # Render any ```chart blocks into PNGs next to the report.
            if self.cfg.enable_charts:
                try:
                    from .visuals import render_report_charts

                    md0 = path.read_text(encoding="utf-8")
                    md1 = render_report_charts(md0, reports_dir, path.stem)
                    if md1 != md0:
                        path.write_text(md1, encoding="utf-8")
                        self._report_text = md1
                        self._report().update(md1)
                except Exception:
                    pass
            self._final_path = str(path)
            self._pct = 100.0
            self._log(f"[green]✔ Done. Saved to {_esc(str(path))}[/green]")
            self._save_extras(path, report, topic)
            if self.cfg.export_pdf:
                self.action_export_pdf()
            if self._result:
                cred = self._result.get("credibility")
                if cred:
                    self._log(f"[dim]sources: {_esc(cred)}[/dim]")
                cost = estimate_cost(self._result.get("usage", {}))
                if cost:
                    self._log(f"[dim]{_esc(cost)}[/dim]")
            try:
                self._log(f"[dim]{_esc(backend.usage_line())}[/dim]")
            except Exception:
                pass
            self._log("[dim]Ctrl+S copy · Ctrl+E export all · Ctrl+O open · Ctrl+N new topic[/dim]")
        except Exception as exc:
            self._log(f"[red]✗ Error: {_esc(str(exc))}[/red]")
            self._report().update(f"# Something went wrong\n\n```\n{exc}\n```")
            self._stage = "error"
        finally:
            if backend is not None:
                try:
                    await backend.aclose()
                except Exception:
                    pass
            self._busy = False
            self._stop_timer()
            inp.disabled = False
            inp.focus()
            self._refresh_status()


def run_tui(cfg: Config, topic: str | None = None) -> None:
    VibeResearchApp(cfg, topic).run()
