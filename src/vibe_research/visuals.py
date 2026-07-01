"""Turn the writer's inline visual specs into real figures in a report.

The synthesizer is asked to emit two kinds of fenced block when the material
warrants it:

* ```chart` blocks holding a small JSON spec (bar/line/pie) — this module
  renders each into a PNG (matplotlib) and swaps the block for an image
  reference, so the chart shows up in the HTML/PDF/DOCX exports.
* ```mermaid` blocks — left untouched here; the HTML export renders them with
  mermaid.js. In Markdown viewers that understand mermaid (GitHub, many editors)
  they render too.

Everything degrades gracefully: if matplotlib isn't installed, or a spec is
malformed, the original ```chart` block is turned into a plain data table (or
left as-is) rather than crashing the run. stdlib-only at import time — matplotlib
is imported lazily inside :func:`render_chart`.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

__all__ = [
    "charts_available",
    "parse_chart_spec",
    "render_chart",
    "render_report_charts",
    "count_words",
]

# Matches a fenced ```chart ... ``` block, capturing its body.
_CHART_BLOCK = re.compile(r"```chart[ \t]*\n(.*?)\n```", re.DOTALL | re.IGNORECASE)
_WORD_RE = re.compile(r"[A-Za-z0-9'’-]+")

_CHART_TYPES = {"bar", "line", "pie", "barh"}


def charts_available() -> bool:
    """True if the chart engine (matplotlib) is importable — probe without it."""
    return importlib.util.find_spec("matplotlib") is not None


def count_words(text: str) -> int:
    """Word count of a report (ignores code fences and markdown punctuation)."""
    stripped = re.sub(r"```.*?```", " ", text or "", flags=re.DOTALL)
    return len(_WORD_RE.findall(stripped))


def _as_floats(values) -> list[float]:
    out: list[float] = []
    for v in values or []:
        try:
            out.append(float(v))
        except (TypeError, ValueError):
            out.append(0.0)
    return out


def parse_chart_spec(body: str) -> dict | None:
    """Parse and normalise a chart spec (JSON). Returns None if unusable.

    Accepts single-series (``labels`` + ``values``) or multi-series
    (``labels`` + ``series: [{name, data}]``).
    """
    try:
        data = json.loads(body)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None

    kind = str(data.get("type", "bar")).strip().lower()
    if kind not in _CHART_TYPES:
        kind = "bar"
    labels = [str(x) for x in (data.get("labels") or [])]

    series: list[dict] = []
    if isinstance(data.get("series"), list):
        for s in data["series"]:
            if isinstance(s, dict) and s.get("data") is not None:
                series.append({"name": str(s.get("name", "")), "data": _as_floats(s["data"])})
    elif data.get("values") is not None:
        series.append({"name": str(data.get("name", "")), "data": _as_floats(data["values"])})

    series = [s for s in series if s["data"]]
    if not series:
        return None
    return {
        "type": kind,
        "title": str(data.get("title", "")).strip(),
        "x_label": str(data.get("x_label", data.get("xlabel", ""))).strip(),
        "y_label": str(data.get("y_label", data.get("ylabel", ""))).strip(),
        "labels": labels,
        "series": series,
    }


def _spec_to_table(spec: dict) -> str:
    """A Markdown table fallback when a chart can't be rendered to an image.

    Loss-less: the row count is driven by the *longest* series (labels are padded
    with 1-based indices), so no data point is dropped even if the model supplied
    fewer labels than data points.
    """
    n = max(len(s["data"]) for s in spec["series"])
    labels = list(spec["labels"])
    labels = (labels + [str(i + 1) for i in range(len(labels), n)])[:n]
    header = "| " + " | ".join(["Category"] + [s["name"] or f"Series {i+1}"
                                               for i, s in enumerate(spec["series"])]) + " |"
    divider = "| " + " | ".join(["---"] * (len(spec["series"]) + 1)) + " |"
    rows = []
    for i in range(n):
        cells = [labels[i]] + [
            f"{s['data'][i]:g}" if i < len(s["data"]) else "" for s in spec["series"]
        ]
        rows.append("| " + " | ".join(cells) + " |")
    title = f"**{spec['title']}**\n\n" if spec["title"] else ""
    return title + "\n".join([header, divider, *rows])


def render_chart(spec: dict, out_path: Path | str) -> Path | None:
    """Render a normalised chart spec to a PNG. Returns the path, or None on failure."""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return None

    try:
        labels = spec["labels"]
        series = spec["series"]
        n = max(len(s["data"]) for s in series)
        labels = (labels + [str(i + 1) for i in range(len(labels), n)])[:n]

        fig, ax = plt.subplots(figsize=(7.5, 4.2))
        kind = spec["type"]

        if kind == "pie":
            data = series[0]["data"][:n]
            ax.pie(data, labels=labels[: len(data)], autopct="%1.0f%%", startangle=90)
            ax.axis("equal")
        elif kind == "line":
            for s in series:
                ax.plot(range(n), (s["data"] + [0] * n)[:n], marker="o", label=s["name"])
            ax.set_xticks(range(n))
            ax.set_xticklabels(labels, rotation=30, ha="right")
        elif kind == "barh":
            import numpy as _np

            width = 0.8 / len(series)
            base = _np.arange(n)
            for i, s in enumerate(series):
                ax.barh(base + i * width, (s["data"] + [0] * n)[:n], height=width, label=s["name"])
            ax.set_yticks(base + 0.4 - width / 2)
            ax.set_yticklabels(labels)
        else:  # bar (grouped)
            import numpy as _np

            width = 0.8 / len(series)
            base = _np.arange(n)
            for i, s in enumerate(series):
                ax.bar(base + i * width, (s["data"] + [0] * n)[:n], width=width, label=s["name"])
            ax.set_xticks(base + 0.4 - width / 2)
            ax.set_xticklabels(labels, rotation=30, ha="right")

        if spec["title"]:
            ax.set_title(spec["title"])
        if spec["x_label"] and kind != "pie":
            ax.set_xlabel(spec["x_label"])
        if spec["y_label"] and kind not in ("pie", "barh"):
            ax.set_ylabel(spec["y_label"])
        if kind != "pie" and (len(series) > 1 or series[0]["name"]):
            ax.legend()
        fig.tight_layout()

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(out_path), dpi=130)
        plt.close(fig)
        return out_path
    except Exception:
        try:
            plt.close("all")
        except Exception:
            pass
        return None


def render_report_charts(report: str, base_dir: Path | str, stem: str) -> str:
    """Replace every ```chart` block with a rendered PNG image ref (or a table).

    PNGs are written to ``base_dir`` as ``<stem>-chart-N.png``. If matplotlib is
    missing or a spec is malformed, the block becomes a Markdown data table so no
    information is lost.
    """
    base_dir = Path(base_dir)
    counter = {"n": 0}

    def _replace(match: re.Match) -> str:
        spec = parse_chart_spec(match.group(1))
        if spec is None:
            return match.group(0)  # leave unparseable block untouched
        counter["n"] += 1
        img = base_dir / f"{stem}-chart-{counter['n']}.png"
        rendered = render_chart(spec, img)
        if rendered is not None:
            alt = spec["title"] or f"Chart {counter['n']}"
            return f"![{alt}]({rendered.name})"
        return _spec_to_table(spec)  # matplotlib unavailable -> table fallback

    return _CHART_BLOCK.sub(_replace, report or "")
