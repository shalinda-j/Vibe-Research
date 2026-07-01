"""Command-line entrypoint for vibe-research.

Subcommands:
    run       Research a topic (default; `vibe-research "topic"` also works)
    doctor    Check environment, dependencies, and auth
    config    View or edit configuration
    history   List past reports

Backends and the TUI are imported lazily inside handlers, so `doctor`,
`--help`, and `config` work even before textual/anthropic are installed.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import asdict
from pathlib import Path

from . import __version__
from .backends import choose_mode, detect_available
from .config import Config, apply_setting, config_path, default_config, load_config, save_config
from .reports import list_reports

_KNOWN_COMMANDS = {"run", "doctor", "config", "history", "memory"}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vibe-research",
        description="Autonomous, fully-cited research agent for your terminal (TUI + CLI).",
    )
    parser.add_argument("--version", action="version", version=f"vibe-research {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="Research a topic")
    run.add_argument("topic", nargs="+", help="The topic to research")
    run.add_argument("--mode", choices=["auto", "api", "subscription"], help="Override backend mode")
    run.add_argument("--no-tui", action="store_true", help="Run headless (print progress + report path)")
    run.add_argument("--parallel", type=int, help="Concurrent research threads")
    run.add_argument("--subquestions", type=int, help="How many sub-questions to research")
    run.add_argument("--planner-model", help="Model for planning + writing")
    run.add_argument("--worker-model", help="Model for the research/search calls")
    run.add_argument("--output-dir", help="Where to save the report")
    run.add_argument("--iterations", type=int, help="Max self-refining rounds (gap-filling)")
    run.add_argument("--votes", type=int, help="Adversarial fact-checkers per finding")
    run.add_argument("--quality", type=float, help="Editor confidence threshold to stop (0-1)")
    run.add_argument("--no-debate", action="store_true", help="Single fact-check instead of a voting debate")
    run.add_argument("--no-memory", action="store_true", help="Don't recall or persist long-term memory")
    run.add_argument("--no-humanize", action="store_true", help="Skip the human-voice rewrite pass")
    run.add_argument("--pdf", action="store_true", help="Also export the report as a PDF beside the .md file")
    run.add_argument("--html", action="store_true", help="Also export the report as a styled HTML page")
    run.add_argument("--json", dest="json_sidecar", action="store_true", help="Also write a structured JSON sidecar")
    run.add_argument("--open", dest="open_after", action="store_true", help="Open the report when the run finishes")
    run.add_argument("--quiet", action="store_true", help="Only print the saved path(s)")
    run.add_argument("--verbose", action="store_true", help="Print fact-check and editor detail")
    run.add_argument("--retries", type=int, help="Backoff retries per model call (0 = none)")
    run.add_argument("--timeout", type=int, help="Per-call timeout in seconds (0 = none)")
    run.add_argument("--concurrency", type=int, help="Max simultaneous model calls")
    run.add_argument("--depth", choices=["quick", "standard", "deep"],
                     help="Preset for sub-questions/votes/iterations")
    run.add_argument("--citations", choices=["ranked", "plain"],
                     help="Reference list style (ranked by credibility, or plain)")
    run.add_argument("--since", type=int, metavar="YEAR", help="Prefer sources from this year onward")
    run.add_argument("--only-domains", help="Comma-sep domain substrings to keep (e.g. gov,edu)")
    run.add_argument("--block-domains", help="Comma-sep domain substrings to drop (e.g. reddit.com)")
    run.add_argument("--verifier-model", help="Model for fact-checking (default: planner model)")
    run.add_argument("--writer-model", help="Model for the write-up (default: planner model)")
    run.add_argument("--humanizer-model", help="Model for the human-voice rewrite (default: planner model)")
    run.add_argument("--docx", action="store_true", help="Also export the report as a Word .docx")
    run.add_argument("--debug", action="store_true", help="Write a JSONL trace of every model call")

    sub.add_parser("doctor", help="Check environment, dependencies, and auth")

    cfg = sub.add_parser("config", help="View or edit configuration")
    cfg.add_argument("--set", metavar="KEY=VALUE", action="append", default=[], help="Set a config value")
    cfg.add_argument("--reset", action="store_true", help="Reset config to defaults")

    hist = sub.add_parser("history", help="List past reports")
    hist.add_argument("-n", type=int, default=20, help="How many to list")

    mem = sub.add_parser("memory", help="Inspect or clear long-term research memory")
    mem.add_argument("--clear", action="store_true", help="Delete all stored memory records")
    mem.add_argument("-n", type=int, default=20, help="How many records to list")

    return parser


_DEPTH_PRESETS = {
    "quick":    {"subquestions": 3, "verifier_votes": 1, "max_iterations": 1},
    "standard": {"subquestions": 5, "verifier_votes": 2, "max_iterations": 2},
    "deep":     {"subquestions": 8, "verifier_votes": 3, "max_iterations": 3},
}


def _cfg_from_args(cfg: Config, args: argparse.Namespace) -> Config:
    # Depth preset first, so explicit --subquestions/--votes/--iterations win.
    depth = getattr(args, "depth", None)
    if depth in _DEPTH_PRESETS:
        for key, value in _DEPTH_PRESETS[depth].items():
            setattr(cfg, key, value)

    if getattr(args, "mode", None):
        cfg.mode = args.mode
    if getattr(args, "parallel", None) is not None:
        cfg.max_parallel = args.parallel
    if getattr(args, "subquestions", None) is not None:
        cfg.subquestions = args.subquestions
    if getattr(args, "planner_model", None):
        cfg.planner_model = args.planner_model
    if getattr(args, "worker_model", None):
        cfg.worker_model = args.worker_model
    if getattr(args, "output_dir", None):
        cfg.reports_dir = args.output_dir
    if getattr(args, "iterations", None) is not None:
        cfg.max_iterations = args.iterations
    if getattr(args, "votes", None) is not None:
        cfg.verifier_votes = args.votes
    if getattr(args, "quality", None) is not None:
        cfg.quality_threshold = args.quality
    if getattr(args, "no_debate", False):
        cfg.enable_debate = False
    if getattr(args, "no_memory", False):
        cfg.enable_memory = False
    if getattr(args, "no_humanize", False):
        cfg.humanize = False
    if getattr(args, "pdf", False):
        cfg.export_pdf = True
    if getattr(args, "html", False):
        cfg.export_html = True
    if getattr(args, "json_sidecar", False):
        cfg.export_json = True
    if getattr(args, "open_after", False):
        cfg.open_after = True
    if getattr(args, "retries", None) is not None:
        cfg.max_retries = args.retries
    if getattr(args, "timeout", None) is not None:
        cfg.call_timeout = args.timeout
    if getattr(args, "concurrency", None) is not None:
        cfg.max_concurrency = args.concurrency
    if getattr(args, "citations", None):
        cfg.citations = args.citations
    if getattr(args, "since", None) is not None:
        cfg.since_year = args.since
    if getattr(args, "only_domains", None):
        cfg.only_domains = args.only_domains
    if getattr(args, "block_domains", None):
        cfg.block_domains = args.block_domains
    if getattr(args, "verifier_model", None):
        cfg.verifier_model = args.verifier_model
    if getattr(args, "writer_model", None):
        cfg.writer_model = args.writer_model
    if getattr(args, "humanizer_model", None):
        cfg.humanizer_model = args.humanizer_model
    if getattr(args, "docx", False):
        cfg.export_docx = True
    if getattr(args, "debug", False):
        cfg.debug = True
    return cfg


def cmd_doctor() -> int:
    import platform

    avail = detect_available()
    mark = lambda ok: "OK " if ok else "-- "  # noqa: E731

    print(f"vibe-research {__version__}")
    print(f"  python              {platform.python_version()}")
    print(f"  textual pkg     [{mark(avail['textual'])}] needed for the TUI")
    print(f"  anthropic pkg   [{mark(avail['anthropic'])}] needed for API mode")
    print(f"  claude-agent-sdk[{mark(avail['claude_agent_sdk'])}] needed for subscription mode")
    print(f"  ANTHROPIC_API_KEY[{mark(avail['api_key_set'])}] set in environment")
    try:
        print(f"  auto mode -> would use: {choose_mode('auto')}")
    except Exception as exc:
        print(f"  auto mode -> none available:\n    {str(exc).splitlines()[0]}")
    print(f"  config file: {config_path()}")

    cfg = load_config()
    print()
    print("Multi-agent pipeline:")
    print(f"  crew            planner · researcher · verifier · editor · writer · humanizer")
    print(f"  self-refine     up to {cfg.max_iterations} round(s), stop at "
          f"confidence >= {cfg.quality_threshold:.0%}")
    print(f"  fact-check      {'debate, ' + str(cfg.verifier_votes) + ' votes' if cfg.enable_debate else 'single verifier'} per finding")
    print(f"  humanize        {'ON — natural human-voice rewrite' if cfg.humanize else 'off'}")
    print(f"  memory          {'ON — ' + str(cfg.resolved_memory_dir()) if cfg.enable_memory else 'off'}")
    print(f"  citations       {cfg.citations} references"
          + (f" · since {cfg.since_year}" if cfg.since_year else ""))
    print(f"  reliability     retry x{cfg.max_retries} · timeout {cfg.call_timeout}s · "
          f"max {cfg.max_concurrency} concurrent")

    from .export import docx_available, pdf_available
    pdf_ok, docx_ok = pdf_available(), docx_available()
    print(f"  pdf export      [{mark(pdf_ok)}] {'ready (run/TUI: --pdf, Ctrl+P)' if pdf_ok else 'needs: pip install fpdf2'}")
    print(f"  docx export     [{mark(docx_ok)}] {'ready (run: --docx)' if docx_ok else 'needs: pip install python-docx'}")
    print(f"  html/json       [OK ] always available (run: --html, --json)")
    print()
    print("Setup tips:")
    print("  API mode:          pip install anthropic ; export ANTHROPIC_API_KEY=sk-ant-...")
    print("  Subscription mode: npm i -g @anthropic-ai/claude-code ; claude (/login) ;")
    print("                     pip install claude-agent-sdk ; unset ANTHROPIC_API_KEY")
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    cfg = load_config()
    if args.reset:
        cfg = default_config()
        save_config(cfg)
        print("Config reset to defaults.")

    changed = False
    for pair in args.set:
        if "=" not in pair:
            print(f"Ignoring '{pair}' (expected KEY=VALUE).")
            continue
        key, value = pair.split("=", 1)
        try:
            apply_setting(cfg, key.strip(), value.strip())
            changed = True
        except KeyError:
            print(f"Unknown config key: {key.strip()}")
            return 2
        except ValueError:
            print(f"Invalid value for {key.strip()}: {value.strip()}")
            return 2
    if changed:
        save_config(cfg)
        print("Saved.")

    print(f"config file: {config_path()}")
    for key, value in asdict(cfg).items():
        print(f"  {key} = {value!r}")
    print(f"  (reports_dir resolves to: {cfg.resolved_reports_dir()})")
    return 0


def cmd_memory(args: argparse.Namespace) -> int:
    from .memory import Memory

    cfg = load_config()
    store = Memory(cfg.resolved_memory_dir())
    if args.clear:
        removed = store.clear()
        print(f"Cleared {removed} memory record(s) from {cfg.resolved_memory_dir()}")
        return 0

    records = store.all()
    if not records:
        print(f"No memory yet in {cfg.resolved_memory_dir()}")
        print("(memory fills up as you run research; disable with `config --set enable_memory=false`)")
        return 0
    print(f"Long-term memory ({len(records)} record(s)) in {cfg.resolved_memory_dir()}:\n")
    for rec in records[: args.n]:
        when = rec.created[:16].replace("T", " ")
        print(f"  [{when}]  {rec.topic}")
        print(f"      confidence {rec.confidence:.0%} · {len(rec.subquestions)} sub-questions · {len(rec.sources)} sources")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    cfg = load_config()
    reports_dir = cfg.resolved_reports_dir()
    reports = list_reports(reports_dir)
    if not reports:
        print(f"No reports yet in {reports_dir}")
        return 0
    print(f"Reports in {reports_dir}:\n")
    for path in reports[: args.n]:
        print(f"  {path.name}")
    return 0


def _open_file(path: Path) -> None:
    """Open a file with the OS default application (best effort)."""
    import subprocess

    try:
        if sys.platform == "win32":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except Exception as exc:
        print(f"⚠ Could not open {path}: {exc}")


def _cost_estimate(usage: dict) -> str:
    """A rough $ estimate from token counts (API mode only). Clearly approximate:
    the real cost depends on which model ran each stage."""
    it = int(usage.get("input_tokens", 0) or 0)
    ot = int(usage.get("output_tokens", 0) or 0)
    if not (it or ot):
        return ""  # subscription mode / no per-token data
    cost = it / 1_000_000 * 5.0 + ot / 1_000_000 * 15.0  # blended Opus-class rate
    return f"est. cost ~${cost:.2f}  ({it:,} in + {ot:,} out tokens, rough)"


def _run_headless(cfg: Config, topic: str, *, quiet: bool = False, verbose: bool = False) -> int:
    from .backends import get_backend
    from .pipeline import run_kwargs_from_config, run_pipeline
    from .reports import save_json, save_report

    result_holder: dict = {}

    async def go() -> int:
        try:
            backend = get_backend(cfg.mode)
        except Exception as exc:
            print(f"[setup] {exc}")
            return 2
        backend.configure(
            max_retries=cfg.max_retries,
            call_timeout=cfg.call_timeout,
            max_concurrency=cfg.max_concurrency,
        )
        if cfg.debug:
            from datetime import datetime

            reports_dir = cfg.resolved_reports_dir()
            reports_dir.mkdir(parents=True, exist_ok=True)
            dbg = reports_dir / f"vibe-debug-{datetime.now():%Y%m%d-%H%M%S}.jsonl"
            backend.configure(debug_path=dbg)
            if not quiet:
                print(f"🐞 debug trace: {dbg}")

        def on_event(kind: str, data: dict) -> None:
            if kind == "done":
                result_holder["result"] = data.get("result", {})
                if not quiet:
                    print(f"\n★ overall confidence: {data.get('confidence', 0):.0%}")
                return
            if quiet:
                return
            if kind == "memory" and data.get("recalled"):
                print(f"🧠 recalled {data['recalled']} related past run(s): "
                      f"{', '.join(data.get('topics', []))[:80]}")
            elif kind == "stage":
                print(f"… {data['msg']}")
            elif kind == "plan":
                for question in data["questions"]:
                    print(f"   • {question}")
            elif kind == "finding":
                print(f"   ✓ {data['question'][:70]} ({data['n_sources']} sources)")
            elif kind == "iteration":
                print(f"↻ round {data['round']}/{data['max']} — closing "
                      f"{len(data.get('gaps', []))} gap(s)")
            elif kind == "critique":
                verdict = "approved" if data["approved"] else "needs more"
                print(f"   ✎ editor: {verdict} (quality {data['quality']:.0%}, "
                      f"confidence {data['confidence']:.0%})")
                if verbose:
                    for miss in data.get("missing", []):
                        print(f"       gap → {miss}")
            elif kind == "debate" and verbose:
                print(f"   ⚖  {data['question'][:60]} → confidence {data['confidence']:.0%}"
                      f" ({data['votes']} votes)")
            elif kind == "verify" and verbose:
                for line in (data.get("review") or "").splitlines():
                    print(f"     {line}")

        try:
            report = await run_pipeline(
                backend,
                topic,
                on_event=on_event,
                **run_kwargs_from_config(cfg),
            )
        finally:
            await backend.aclose()

        result = result_holder.get("result") or None
        reports_dir = cfg.resolved_reports_dir()
        path = save_report(reports_dir, topic, report, meta=result)
        saved_md = path.read_text(encoding="utf-8")
        outputs: list[tuple[str, Path]] = [("Report", path)]

        if cfg.export_json and result:
            outputs.append(("JSON", save_json(path, result)))
        if cfg.export_pdf:
            from .export import markdown_to_pdf, pdf_path_for
            try:
                outputs.append(("PDF", markdown_to_pdf(saved_md, pdf_path_for(path), title=topic)))
            except Exception as exc:
                print(f"⚠ PDF export skipped: {exc}")
        if cfg.export_html:
            from .export import html_path_for, markdown_to_html_file
            try:
                outputs.append(("HTML", markdown_to_html_file(saved_md, html_path_for(path), title=topic)))
            except Exception as exc:
                print(f"⚠ HTML export skipped: {exc}")
        if cfg.export_docx:
            from .export import docx_path_for, markdown_to_docx
            try:
                outputs.append(("DOCX", markdown_to_docx(saved_md, docx_path_for(path), title=topic)))
            except Exception as exc:
                print(f"⚠ DOCX export skipped: {exc}")

        for label, out in outputs:
            print(f"✔ {label}: {out}")
        if not quiet:
            if result and result.get("credibility"):
                print(f"  sources: {result['credibility']}")
            try:
                print(f"  ({backend.usage_line()})")
            except Exception:
                pass
            cost = _cost_estimate(result.get("usage", {})) if result else ""
            if cost:
                print(f"  {cost}")
        if cfg.open_after:
            by_label = dict(outputs)
            _open_file(by_label.get("PDF") or by_label.get("HTML") or by_label["Report"])
        return 0

    try:
        return asyncio.run(go())
    except KeyboardInterrupt:
        print("\n⏹ Interrupted — partial work discarded.")
        return 130


def cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg_from_args(load_config(), args)
    topic = " ".join(args.topic).strip()
    if not topic:
        print("Please provide a topic.")
        return 2

    quiet = getattr(args, "quiet", False)
    verbose = getattr(args, "verbose", False)

    if args.no_tui:
        return _run_headless(cfg, topic, quiet=quiet, verbose=verbose)

    try:
        from .tui import run_tui
    except Exception as exc:
        print(f"TUI unavailable ({exc}). Falling back to headless mode.")
        print("Install the TUI with:  pip install textual")
        return _run_headless(cfg, topic, quiet=quiet, verbose=verbose)

    run_tui(cfg, topic)
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page; the CLI prints Unicode
    # (✓, ⚖, 🧠 …). Reconfigure to UTF-8 so output never crashes when redirected.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:
            pass

    argv = list(sys.argv[1:] if argv is None else argv)
    # Shorthand: `vibe-research "some topic"` -> `vibe-research run "some topic"`
    if argv and argv[0] not in _KNOWN_COMMANDS and not argv[0].startswith("-"):
        argv = ["run"] + argv

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "doctor":
        return cmd_doctor()
    if args.command == "config":
        return cmd_config(args)
    if args.command == "history":
        return cmd_history(args)
    if args.command == "memory":
        return cmd_memory(args)
    if args.command == "run":
        return cmd_run(args)

    parser.print_help()
    return 0
