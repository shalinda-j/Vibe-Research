"""Export a Markdown research report to PDF.

Optional feature (the ``[pdf]`` extra → ``fpdf2``). The report Markdown is
rendered to HTML with markdown-it-py — already present as a Textual dependency,
so the HTML step needs nothing new — and laid out by fpdf2 using a *discovered
system Unicode font*. That means accents, arrows, bullets, em-dashes and the
like render correctly without shipping a font file or pulling in any native
dependency (no GTK/Cairo/Pango), which keeps it painless on Windows.

Everything heavy is imported lazily inside the functions, so ``import
vibe_research.export`` stays cheap and ``pdf_available()`` can be probed without
fpdf2 installed.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

# (regular, bold, italic, bold-italic) TrueType families to try, in order.
_FONT_FAMILIES = (
    ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
    ("segoeui.ttf", "segoeuib.ttf", "segoeuii.ttf", "segoeuiz.ttf"),
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf", "DejaVuSans-BoldOblique.ttf"),
    ("Helvetica.ttc", "Helvetica.ttc", "Helvetica.ttc", "Helvetica.ttc"),
)


def _font_dirs() -> list[Path]:
    if sys.platform == "win32":
        win = Path(__import__("os").environ.get("WINDIR", r"C:\Windows"))
        return [win / "Fonts", Path.home() / "AppData/Local/Microsoft/Windows/Fonts"]
    if sys.platform == "darwin":
        return [Path("/System/Library/Fonts"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"]
    return [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".fonts",
        Path.home() / ".local/share/fonts",
    ]


def _find_unicode_font() -> tuple[Path, Path, Path, Path] | None:
    """Locate a regular/bold/italic/bold-italic Unicode TTF set on this machine."""
    dirs = _font_dirs()
    # Fast path: exact filenames in a known font directory.
    for family in _FONT_FAMILIES:
        for directory in dirs:
            paths = [directory / name for name in family]
            if all(p.exists() for p in paths):
                return paths[0], paths[1], paths[2], paths[3]
    # Slow path: recursive search for DejaVuSans (bundled with many Linux distros).
    for directory in dirs:
        if not directory.exists():
            continue
        try:
            hits = {p.name: p for p in directory.rglob("DejaVuSans*.ttf")}
        except OSError:
            continue
        reg = hits.get("DejaVuSans.ttf")
        if reg:
            return (
                reg,
                hits.get("DejaVuSans-Bold.ttf", reg),
                hits.get("DejaVuSans-Oblique.ttf", reg),
                hits.get("DejaVuSans-BoldOblique.ttf", reg),
            )
    return None


def pdf_available() -> bool:
    """True if the PDF engine (fpdf2) is importable — probe without importing it."""
    return importlib.util.find_spec("fpdf") is not None


def _latin1_safe(text: str) -> str:
    """Drop-in for the no-Unicode-font fallback: keep only Latin-1 renderable chars."""
    return text.encode("latin-1", "replace").decode("latin-1")


def markdown_to_pdf(markdown: str, out_path: Path | str, title: str = "") -> Path:
    """Render a Markdown report to a PDF file and return its path.

    Raises ``RuntimeError`` with install instructions if fpdf2 is missing.
    """
    try:
        from fpdf import FPDF
    except ImportError as exc:  # pragma: no cover - exercised only without the extra
        raise RuntimeError(
            "PDF export needs the 'fpdf2' package.\n"
            "    pip install fpdf2\n"
            '    (or: pip install "vibe-research[pdf]")'
        ) from exc
    from markdown_it import MarkdownIt

    html = MarkdownIt("commonmark").enable("table").render(markdown or "")
    fonts = _find_unicode_font()

    class _ReportPDF(FPDF):
        def footer(self) -> None:
            self.set_y(-12)
            self.set_font(self._body_family, "I", 8)
            self.set_text_color(150, 150, 150)
            self.cell(0, 8, f"vibe-research  ·  page {self.page_no()}/{{nb}}", align="C")
            self.set_text_color(0, 0, 0)

    pdf = _ReportPDF(format="A4")
    pdf._body_family = "helvetica"
    if fonts:
        reg, bold, ital, bolditalic = fonts
        pdf.add_font("report", "", str(reg))
        pdf.add_font("report", "B", str(bold))
        pdf.add_font("report", "I", str(ital))
        pdf.add_font("report", "BI", str(bolditalic))
        pdf._body_family = "report"

    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 16, 18)
    pdf.add_page()

    # --- title block ---------------------------------------------------------
    if title:
        pdf.set_font(pdf._body_family, "B", 19)
        pdf.multi_cell(0, 9, title if fonts else _latin1_safe(title))
        pdf.ln(1)
    pdf.set_font(pdf._body_family, "I", 9)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 6, f"Generated {datetime.now():%Y-%m-%d %H:%M} by vibe-research")
    pdf.set_draw_color(210, 210, 210)
    pdf.line(pdf.l_margin, pdf.get_y() + 1, pdf.w - pdf.r_margin, pdf.get_y() + 1)
    pdf.ln(4)
    pdf.set_text_color(0, 0, 0)

    # --- body ----------------------------------------------------------------
    pdf.set_font(pdf._body_family, "", 11)
    pdf.write_html(html if fonts else _latin1_safe(html))

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out_path))
    return out_path


def pdf_path_for(report_path: Path | str) -> Path:
    """The sibling ``.pdf`` path for a saved ``.md`` report."""
    return Path(report_path).with_suffix(".pdf")


def html_path_for(report_path: Path | str) -> Path:
    """The sibling ``.html`` path for a saved ``.md`` report."""
    return Path(report_path).with_suffix(".html")


_HTML_TEMPLATE = """\
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{
    max-width: 46rem; margin: 2.5rem auto; padding: 0 1.2rem;
    font: 16px/1.65 -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif;
    color: #1a1a1a; background: #fff;
  }}
  h1, h2, h3 {{ line-height: 1.25; margin-top: 1.8em; }}
  h1 {{ font-size: 1.9rem; border-bottom: 2px solid #eee; padding-bottom: .3em; }}
  h2 {{ font-size: 1.4rem; }}
  a {{ color: #2563eb; word-break: break-word; }}
  code {{ background: #f3f3f3; padding: .1em .35em; border-radius: 4px; font-size: .9em; }}
  pre {{ background: #f6f8fa; padding: 1em; border-radius: 8px; overflow-x: auto; }}
  pre code {{ background: none; padding: 0; }}
  blockquote {{ margin: 1em 0; padding: .2em 1em; border-left: 4px solid #d1d5db; color: #555; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: .5em .7em; text-align: left; }}
  th {{ background: #f6f8fa; }}
  hr {{ border: none; border-top: 1px solid #eee; margin: 2em 0; }}
  footer {{ margin-top: 3rem; padding-top: 1rem; border-top: 1px solid #eee;
            color: #888; font-size: .85rem; }}
  @media (prefers-color-scheme: dark) {{
    body {{ color: #e6e6e6; background: #16181d; }}
    h1 {{ border-color: #2a2d34; }} code {{ background: #23262e; }}
    pre {{ background: #1c1f26; }} th {{ background: #23262e; }}
    td, th {{ border-color: #2a2d34; }} a {{ color: #6ea8fe; }}
  }}
</style>
</head>
<body>
{body}
<footer>Generated by vibe-research.</footer>
</body>
</html>
"""


def markdown_to_html(markdown: str, title: str = "") -> str:
    """Render a Markdown report into a standalone, styled HTML document string."""
    from markdown_it import MarkdownIt

    body = MarkdownIt("commonmark").enable("table").render(markdown or "")
    safe_title = (title or "vibe-research report").replace("<", "&lt;").replace(">", "&gt;")
    return _HTML_TEMPLATE.format(title=safe_title, body=body)


def markdown_to_html_file(markdown: str, out_path: Path | str, title: str = "") -> Path:
    """Render a Markdown report to an HTML file and return its path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown_to_html(markdown, title), encoding="utf-8")
    return out_path
