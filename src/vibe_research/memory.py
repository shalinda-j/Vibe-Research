"""Long-term memory for vibe-research.

After every run the orchestrator distils the findings into a
:class:`~vibe_research.schemas.ResearchRecord` and stores it on disk. Before a
new run, the planner recalls related records so it can build on what's already
known instead of starting cold — the closest this tool gets to "learning across
sessions".

One JSON file per topic (keyed by slug), under
``~/.local/share/vibe-research/memory`` by default. stdlib-only, so the offline
test-suite exercises it without any heavy dependency.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from pathlib import Path

from .reports import slug
from .schemas import ResearchRecord


def _key(topic: str) -> str:
    """Filename stem for a topic: a readable slug plus a short hash of the full
    topic. The hash disambiguates distinct topics that slugify identically
    (e.g. 'C++ vs C#' and 'C vs C' both slug to 'c-vs-c'), so remembering one
    never silently overwrites the other."""
    digest = hashlib.sha1(topic.strip().lower().encode("utf-8")).hexdigest()[:8]
    return f"{slug(topic)}-{digest}"

# Words too common to be useful for matching related topics.
_STOPWORDS = {
    "the", "a", "an", "of", "and", "or", "for", "to", "in", "on", "at", "by",
    "with", "from", "about", "into", "over", "between", "is", "are", "was",
    "were", "how", "what", "why", "when", "which", "impact", "effect", "role",
    "vs", "versus", "using", "based",
}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z0-9]+", (text or "").lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


class Memory:
    """A directory of research records with keyword-overlap recall."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------- internals

    def _path(self, topic: str) -> Path:
        return self.root / f"{_key(topic)}.json"

    def _load(self, path: Path) -> ResearchRecord | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return ResearchRecord.from_json(data)
        except Exception:
            return None

    # ---------------------------------------------------------------- public

    def remember(self, record: ResearchRecord) -> Path:
        """Persist a record, stamping ``created`` if the caller left it blank."""
        self.root.mkdir(parents=True, exist_ok=True)
        if not record.created:
            record.created = datetime.now().isoformat(timespec="seconds")
        path = self._path(record.topic)
        # Write atomically (temp file + os.replace) so an interrupted or
        # concurrent write can't leave a truncated, unreadable record behind.
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(record.to_json(), indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return path

    def recall(self, topic: str) -> ResearchRecord | None:
        """Return the record for this exact topic (by slug), if any."""
        path = self._path(topic)
        return self._load(path) if path.exists() else None

    def all(self) -> list[ResearchRecord]:
        """Every stored record, newest first (by ``created``)."""
        if not self.root.exists():
            return []
        records = [rec for p in self.root.glob("*.json") if (rec := self._load(p))]
        return sorted(records, key=lambda r: r.created, reverse=True)

    def related(self, topic: str, limit: int = 3) -> list[ResearchRecord]:
        """Records whose topic/key-points overlap the query, best match first.

        Simple token-overlap scoring — cheap, deterministic, and good enough to
        surface "you researched something adjacent before". The exact-slug match
        for ``topic`` itself is excluded so the planner sees *neighbours*, not a
        copy of the current topic.
        """
        query = _tokens(topic)
        if not query:
            return []
        this_key = _key(topic)
        scored: list[tuple[int, ResearchRecord]] = []
        for rec in self.all():
            if _key(rec.topic) == this_key:
                continue
            haystack = _tokens(rec.topic) | _tokens(" ".join(rec.key_points))
            overlap = len(query & haystack)
            if overlap:
                scored.append((overlap, rec))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [rec for _, rec in scored[: max(0, limit)]]

    def clear(self) -> int:
        """Delete all stored records. Returns how many files were removed."""
        if not self.root.exists():
            return 0
        removed = 0
        for path in self.root.glob("*.json"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed
