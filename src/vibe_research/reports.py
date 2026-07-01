"""Saving and listing research reports."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path


def slug(topic: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "-", topic.lower()).strip("-")
    return cleaned[:60] or "research"


def _meta_line(meta: dict | None, ts: datetime) -> str:
    """A one-line metadata summary rendered as a Markdown blockquote body."""
    parts = [f"**Generated** {ts:%Y-%m-%d %H:%M}"]
    if meta:
        if meta.get("mode"):
            parts.append(f"**mode** {meta['mode']}")
        if meta.get("overall_confidence") is not None:
            parts.append(f"**confidence** {meta['overall_confidence']:.0%}")
        if meta.get("sources") is not None:
            parts.append(f"**sources** {len(meta['sources'])}")
        if meta.get("subquestions") is not None:
            parts.append(f"**sub-questions** {len(meta['subquestions'])}")
    return " · ".join(parts)


def save_report(reports_dir: Path, topic: str, markdown: str, meta: dict | None = None) -> Path:
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now()
    path = reports_dir / f"{ts:%Y%m%d-%H%M%S}-{slug(topic)}.md"

    body = markdown.strip()
    meta_line = _meta_line(meta, ts)
    lines = body.splitlines()
    if lines and lines[0].lstrip().startswith("#"):
        # Keep the report's own H1; slot the metadata line right beneath it.
        head = lines[0]
        rest = "\n".join(lines[1:]).lstrip("\n")
        body = f"{head}\n\n> {meta_line}\n\n{rest}"
    else:
        body = f"# {topic}\n\n> {meta_line}\n\n{body}"
    path.write_text(body.rstrip() + "\n", encoding="utf-8")
    return path


def save_json(report_path: Path | str, result: dict) -> Path:
    """Write a structured JSON sidecar next to a saved ``.md`` report."""
    path = Path(report_path).with_suffix(".json")
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def list_reports(reports_dir: Path) -> list[Path]:
    reports_dir = Path(reports_dir)
    if not reports_dir.exists():
        return []
    return sorted(reports_dir.glob("*.md"), reverse=True)
