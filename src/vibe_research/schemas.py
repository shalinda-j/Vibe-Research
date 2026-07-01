"""Typed, validated data structures for the vibe-research multi-agent pipeline.

Every value that crosses an agent boundary — a plan, a finding, a fact-check
verdict, an editor critique — is parsed into one of these dataclasses and
validated here. LLM output is untrusted text: this module is the gate that turns
it into structured data we can reason about, or rejects it with a clear
``ValidationError``.

stdlib-only on purpose. The offline test-suite, ``doctor`` and ``config`` must
keep working before any heavy dependency is installed, so nothing here imports
``pydantic``/``anthropic``/``textual``. The validation is hand-rolled but strict:
type coercion, bounds checks, URL well-formedness, de-duplication, and salvage of
almost-JSON model output.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "ValidationError",
    "strip_fences",
    "parse_json",
    "clamp01",
    "clean_url",
    "clean_str",
    "clean_url_list",
    "SubQuestion",
    "ResearchPlan",
    "Claim",
    "Finding",
    "Verdict",
    "VerificationReport",
    "Critique",
    "ResearchRecord",
    "VERDICT_STATES",
]


class ValidationError(ValueError):
    """Raised when untrusted (LLM) data fails a schema check."""


# --------------------------------------------------------------------- parsing

# A URL must be http(s), have a dotted host (at least one "."), and no
# whitespace. Kept linear (no nested quantifiers) to avoid catastrophic
# backtracking on long adversarial input.
_URL_RE = re.compile(
    r"^https?://[^\s/$.?#]+(?:\.[^\s/$.?#]+)+(?:[/?#]\S*)?$", re.IGNORECASE
)
_FENCE_RE = re.compile(r"^```(?:json)?[ \t]*\n?|\n?```$", re.MULTILINE)


def strip_fences(text: str) -> str:
    """Remove leading/trailing Markdown code fences from model output."""
    return _FENCE_RE.sub("", (text or "").strip()).strip()


def parse_json(text: str) -> Any:
    """Parse a JSON value out of possibly-fenced, possibly-chatty model output.

    Tries the whole (de-fenced) string first, then salvages the first balanced
    ``{...}`` or ``[...]`` span. Raises :class:`ValidationError` if nothing
    parses — callers decide whether to fall back or fail.
    """
    raw = strip_fences(text)
    if not raw:
        raise ValidationError("empty model output; nothing to parse")
    try:
        return json.loads(raw)
    except Exception:
        pass
    # Salvage: scan for the first *complete, balanced* JSON value. Using
    # raw_decode at each opener (rather than first-'{' .. last-'}') correctly
    # recovers the first object even when the model prepends prose containing
    # braces or emits several JSON blocks in one response.
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char in "{[":
            try:
                value, _ = decoder.raw_decode(raw, index)
                return value
            except ValueError:
                continue
    raise ValidationError("could not parse JSON from model output")


# ------------------------------------------------------------------- coercion


def clamp01(value: Any, default: float = 0.5) -> float:
    """Coerce ``value`` to a float in ``[0, 1]``; return ``default`` if unusable."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    if f != f:  # NaN
        return default
    return max(0.0, min(1.0, f))


def clean_str(value: Any) -> str:
    """Best-effort string, stripped. ``None`` -> ``""``."""
    if value is None:
        return ""
    return str(value).strip()


def _as_list(value: Any) -> list:
    """Coerce to a list, else empty. Guards against a model returning a scalar
    (e.g. ``{"gaps": 5}``) for a field we iterate — a bare number would raise
    TypeError, and a bare string would iterate character-by-character."""
    return list(value) if isinstance(value, (list, tuple)) else []


def clean_url(url: Any) -> str | None:
    """Return a normalised http(s) URL, or ``None`` if it isn't a valid one."""
    if not isinstance(url, str):
        return None
    u = url.strip().rstrip(".,;:")
    # Trim one layer of wrapping brackets/quotes if present.
    u = u.strip("\"'<>")
    return u if _URL_RE.match(u) else None


def clean_url_list(values: Any) -> list[str]:
    """Coerce an arbitrary value into a de-duplicated list of valid URLs."""
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        if isinstance(item, dict):
            item = item.get("url") or item.get("href") or item.get("link")
        url = clean_url(item)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ------------------------------------------------------------------- entities

VERDICT_STATES = (
    "supported",
    "partly-supported",
    "unsupported",
    "contradicted",
    "uncertain",
)
_VERDICT_ALIASES = {
    "support": "supported",
    "supported": "supported",
    "true": "supported",
    "verified": "supported",
    "partial": "partly-supported",
    "partly": "partly-supported",
    "partly-supported": "partly-supported",
    "weak": "partly-supported",
    "unsupported": "unsupported",
    "unsourced": "unsupported",
    "none": "unsupported",
    "false": "contradicted",
    "contradicted": "contradicted",
    "conflict": "contradicted",
    "uncertain": "uncertain",
    "unknown": "uncertain",
    "unclear": "uncertain",
}


def normalise_status(value: Any) -> str:
    key = clean_str(value).lower().replace(" ", "-")
    return _VERDICT_ALIASES.get(key, "uncertain")


@dataclass
class SubQuestion:
    """One focused research sub-question with an optional rationale."""

    text: str
    rationale: str = ""

    @classmethod
    def coerce(cls, data: Any) -> "SubQuestion":
        if isinstance(data, str):
            return cls(text=clean_str(data))
        if isinstance(data, dict):
            text = clean_str(
                data.get("text") or data.get("question") or data.get("q") or data.get("title")
            )
            return cls(text=text, rationale=clean_str(data.get("rationale") or data.get("why")))
        raise ValidationError(f"cannot read a sub-question from {type(data).__name__}")

    def validate(self) -> "SubQuestion":
        if len(self.text) < 6:
            raise ValidationError(f"sub-question too short: {self.text!r}")
        self.text = self.text[:400]
        return self


@dataclass
class ResearchPlan:
    """A validated set of non-overlapping sub-questions for a topic."""

    topic: str
    subquestions: list[SubQuestion] = field(default_factory=list)

    @classmethod
    def parse(cls, topic: str, text: str, *, hard_cap: int = 12) -> "ResearchPlan":
        data = parse_json(text)
        items: Any = data
        if isinstance(data, dict):
            items = (
                data.get("subquestions")
                or data.get("questions")
                or data.get("plan")
                or data.get("items")
                or []
            )
        if not isinstance(items, list):
            raise ValidationError("plan is not a list of sub-questions")

        seen: set[str] = set()
        subs: list[SubQuestion] = []
        for item in items:
            try:
                sq = SubQuestion.coerce(item).validate()
            except ValidationError:
                continue
            key = sq.text.lower()
            if key in seen:
                continue
            seen.add(key)
            subs.append(sq)

        if not subs:
            raise ValidationError("plan produced no usable sub-questions")
        return cls(topic=topic, subquestions=subs[: max(1, hard_cap)])

    @property
    def questions(self) -> list[str]:
        return [sq.text for sq in self.subquestions]


@dataclass
class Claim:
    """A single factual assertion and the sources offered for it."""

    text: str
    sources: list[str] = field(default_factory=list)


@dataclass
class Finding:
    """One researched sub-question: the answer, its sources, and a confidence.

    ``confidence`` starts as the researcher's self-assessment and is overwritten
    by the aggregated fact-check score once verification runs.
    """

    question: str
    answer: str
    sources: list[str] = field(default_factory=list)
    confidence: float = 0.5

    @classmethod
    def build(cls, question: str, answer: str, sources: Any, confidence: Any = 0.5) -> "Finding":
        return cls(
            question=clean_str(question),
            answer=clean_str(answer),
            sources=clean_url_list(sources),
            confidence=clamp01(confidence),
        )


@dataclass
class Verdict:
    """A fact-checker's judgement on one claim."""

    claim: str
    status: str  # one of VERDICT_STATES
    confidence: float = 0.5
    note: str = ""

    @classmethod
    def coerce(cls, data: Any) -> "Verdict":
        if not isinstance(data, dict):
            raise ValidationError("verdict must be an object")
        claim = clean_str(data.get("claim") or data.get("text") or data.get("statement"))
        if not claim:
            raise ValidationError("verdict has no claim text")
        return cls(
            claim=claim[:600],
            status=normalise_status(data.get("status") or data.get("verdict")),
            confidence=clamp01(data.get("confidence"), default=0.5),
            note=clean_str(data.get("note") or data.get("reason"))[:600],
        )


@dataclass
class VerificationReport:
    """The aggregate of one or more fact-checkers over a single finding."""

    verdicts: list[Verdict] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    contradictions: list[str] = field(default_factory=list)
    overall_confidence: float = 0.5

    @classmethod
    def parse(cls, text: str) -> "VerificationReport":
        data = parse_json(text)
        if not isinstance(data, dict):
            raise ValidationError("verification must be a JSON object")
        verdicts: list[Verdict] = []
        for item in _as_list(data.get("verdicts") or data.get("claims")):
            try:
                verdicts.append(Verdict.coerce(item))
            except ValidationError:
                continue
        gaps = [clean_str(g) for g in _as_list(data.get("gaps")) if clean_str(g)]
        contradictions = [
            clean_str(c) for c in _as_list(data.get("contradictions")) if clean_str(c)
        ]
        if "overall_confidence" in data or "confidence" in data:
            overall = clamp01(data.get("overall_confidence", data.get("confidence")))
        elif verdicts:
            overall = round(sum(v.confidence for v in verdicts) / len(verdicts), 3)
        else:
            overall = 0.5
        return cls(
            verdicts=verdicts,
            gaps=gaps[:20],
            contradictions=contradictions[:20],
            overall_confidence=overall,
        )

    @property
    def weak_claims(self) -> list[Verdict]:
        return [v for v in self.verdicts if v.status in ("unsupported", "contradicted")]


def aggregate_verifications(reports: list["VerificationReport"]) -> "VerificationReport":
    """Combine independent fact-checkers into one consensus (the 'debate' vote).

    Confidence is averaged (the vote); gaps and contradictions are unioned so no
    single skeptic's concern is lost; verdicts are concatenated. An empty input
    yields a neutral report rather than raising, so the caller never crashes on a
    round where every verifier failed to parse.
    """
    reports = [r for r in reports if r is not None]
    if not reports:
        return VerificationReport(overall_confidence=0.5)
    verdicts: list[Verdict] = []
    gaps: list[str] = []
    contradictions: list[str] = []
    seen_gap: set[str] = set()
    seen_con: set[str] = set()
    for r in reports:
        verdicts.extend(r.verdicts)
        for g in r.gaps:
            if g.lower() not in seen_gap:
                seen_gap.add(g.lower())
                gaps.append(g)
        for c in r.contradictions:
            if c.lower() not in seen_con:
                seen_con.add(c.lower())
                contradictions.append(c)
    overall = round(sum(r.overall_confidence for r in reports) / len(reports), 3)
    return VerificationReport(
        verdicts=verdicts,
        gaps=gaps[:20],
        contradictions=contradictions[:20],
        overall_confidence=overall,
    )


@dataclass
class Critique:
    """The editor's verdict on the whole body of findings.

    ``missing`` items become new sub-questions on the next iteration — this is
    the feedback signal that drives the self-refining loop.
    """

    approved: bool = False
    quality: float = 0.5
    missing: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    note: str = ""

    @classmethod
    def parse(cls, text: str) -> "Critique":
        data = parse_json(text)
        if not isinstance(data, dict):
            raise ValidationError("critique must be a JSON object")
        missing = [clean_str(m) for m in _as_list(data.get("missing") or data.get("gaps")) if clean_str(m)]
        issues = [clean_str(i) for i in _as_list(data.get("issues")) if clean_str(i)]
        return cls(
            approved=bool(data.get("approved", False)),
            quality=clamp01(data.get("quality", data.get("score")), default=0.5),
            missing=missing[:12],
            issues=issues[:20],
            note=clean_str(data.get("note") or data.get("summary"))[:800],
        )


@dataclass
class ResearchRecord:
    """What we persist to long-term memory after a completed run."""

    topic: str
    created: str = ""
    subquestions: list[str] = field(default_factory=list)
    key_points: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    confidence: float = 0.5
    report_path: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, data: Any) -> "ResearchRecord":
        if not isinstance(data, dict):
            raise ValidationError("memory record must be a JSON object")
        return cls(
            topic=clean_str(data.get("topic")),
            created=clean_str(data.get("created")),
            subquestions=[clean_str(s) for s in _as_list(data.get("subquestions")) if clean_str(s)],
            key_points=[clean_str(k) for k in _as_list(data.get("key_points")) if clean_str(k)],
            sources=clean_url_list(data.get("sources")),
            confidence=clamp01(data.get("confidence")),
            report_path=clean_str(data.get("report_path")),
        )
