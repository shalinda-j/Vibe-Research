"""Report enrichment: source credibility, ranked references, disagreement
surfacing, and source-domain filtering.

All pure stdlib and side-effect free, so the offline test-suite covers it fully.
The heuristics are deliberately simple and transparent — a domain-signal lookup,
not a model call — because the point is a *fast, explainable* credibility hint
the reader can sanity-check, not a verdict to trust blindly.
"""

from __future__ import annotations

from urllib.parse import urlparse

__all__ = [
    "domain_of",
    "score_source",
    "rank_sources",
    "sources_section",
    "disagreements_section",
    "filter_sources",
    "credibility_summary",
]

# Host substrings that signal a primary / authoritative source.
_PRIMARY_TLDS = (".gov", ".edu", ".mil", ".int", ".ac")
_ACADEMIC = (
    "ncbi.nlm.nih.gov", "pubmed", "nih.gov", "who.int", "cdc.gov", "nature.com",
    "science.org", "sciencedirect.com", "springer", "wiley.com", "cell.com",
    "thelancet.com", "bmj.com", "nejm.org", "jstor.org", "arxiv.org", "ieee.org",
    "acm.org", "oecd.org", "imf.org", "worldbank.org", "un.org", "europa.eu",
    # Clinical / pharmaceutical / regulatory sources — authoritative for medical
    # research (drugs, trials, safety) and useful generally.
    "clinicaltrials.gov", "cochrane", "cochranelibrary.com", "fda.gov",
    "ema.europa.eu", "drugbank", "medlineplus.gov", "jamanetwork.com",
    "biorxiv.org", "medrxiv.org", "ahajournals.org", "academic.oup.com",
    "plos.org", "ecdc.europa.eu", "nice.org.uk",
)
_NEWS = (
    "reuters.com", "apnews.com", "bbc.co", "bbc.com", "nytimes.com", "wsj.com",
    "economist.com", "ft.com", "bloomberg.com", "theguardian.com", "npr.org",
    "washingtonpost.com", "aljazeera.com", "cnbc.com", "forbes.com", "time.com",
)
_LOW = (
    "blogspot", "wordpress.com", "medium.com", "reddit.com", "quora.com",
    "facebook.com", "twitter.com", "x.com", "pinterest", "tiktok", "youtube.com",
    "substack.com", "fandom.com", "answers.com", "ehow", "wikihow",
)


def domain_of(url: str) -> str:
    """The lower-cased host of a URL, minus a leading ``www.`` (``""`` on junk)."""
    try:
        host = urlparse(url).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _is_primary(host: str) -> bool:
    return any(host.endswith(t) or (t + ".") in host for t in _PRIMARY_TLDS)


def score_source(url: str) -> dict:
    """Classify one source URL into a credibility tier with a 0-1 score + label."""
    host = domain_of(url)

    def has(table) -> bool:
        # Match the HOST only — never the path/query, or a junk URL that merely
        # embeds an authoritative domain (a redirect target, a share/tracking
        # param) would be falsely elevated.
        return any(s in host for s in table)

    if _is_primary(host) or has(_ACADEMIC):
        tier, score, label = "high", 0.95, "primary / authoritative"
    elif has(_NEWS):
        tier, score, label = "medium-high", 0.75, "reputable news"
    elif host.endswith(".org"):
        tier, score, label = "medium", 0.60, "organisation"
    elif has(_LOW):
        tier, score, label = "low", 0.30, "blog / social / user-generated"
    elif host:
        tier, score, label = "medium", 0.50, "general web"
    else:
        tier, score, label = "unknown", 0.40, "unrecognised"
    return {"url": url, "domain": host, "tier": tier, "score": score, "label": label}


def rank_sources(urls: list[str]) -> list[dict]:
    """Score sources and return them most-credible first (stable for ties)."""
    scored = [score_source(u) for u in dict.fromkeys(urls)]  # de-dup, keep order
    return sorted(scored, key=lambda s: s["score"], reverse=True)


def sources_section(urls: list[str], title: str = "Sources") -> str:
    """A numbered, credibility-ranked reference list. ``""`` if there are none."""
    ranked = rank_sources(urls)
    if not ranked:
        return ""
    lines = [f"## {title}", ""]
    for i, s in enumerate(ranked, 1):
        lines.append(f"{i}. {s['url']} — _{s['label']}_")
    return "\n".join(lines)


def credibility_summary(urls: list[str]) -> str:
    """A one-line tally like ``3 high · 2 medium · 1 low credibility``."""
    ranked = rank_sources(urls)
    if not ranked:
        return ""
    buckets = {"high": 0, "medium-high": 0, "medium": 0, "low": 0, "unknown": 0}
    for s in ranked:
        buckets[s["tier"]] = buckets.get(s["tier"], 0) + 1
    parts = [f"{n} {tier}" for tier, n in buckets.items() if n]
    return " · ".join(parts) + " credibility" if parts else ""


def disagreements_section(items: list[str]) -> str:
    """A section listing contested/conflicting points. ``""`` if there are none."""
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        text = (item or "").strip()
        if text and text.lower() not in seen:
            seen.add(text.lower())
            unique.append(text)
    if not unique:
        return ""
    lines = [
        "## Disagreements & Conflicts",
        "",
        "The fact-checkers flagged points where sources conflict or the evidence "
        "is contested — weigh these carefully:",
        "",
    ]
    lines += [f"- {item}" for item in unique]
    return "\n".join(lines)


def filter_sources(
    urls: list[str],
    only: list[str] | None = None,
    block: list[str] | None = None,
) -> list[str]:
    """Keep only URLs whose host matches ``only`` (if given) and avoids ``block``.

    ``only``/``block`` are lists of case-insensitive domain substrings, e.g.
    ``["gov", "edu"]`` or ``["reddit.com"]``. Empty/blank entries are ignored.
    """
    only = [o.lower() for o in (only or []) if o and o.strip()]
    block = [b.lower() for b in (block or []) if b and b.strip()]
    out: list[str] = []
    for url in urls:
        host = domain_of(url)
        if block and any(b in host for b in block):
            continue
        if only and not any(o in host for o in only):
            continue
        out.append(url)
    return out
