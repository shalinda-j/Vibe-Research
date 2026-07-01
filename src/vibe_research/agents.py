"""The agent roles that make up the vibe-research crew.

Each agent is a thin, single-responsibility wrapper around a :class:`Backend`:
it owns one prompt, one model, and one *validated* output type. That separation
is what turns the old linear pipeline into a genuine multi-agent system —
roles can be run in parallel, voted against each other (the fact-check
"debate"), and looped (the editor's critique feeds back into planning).

Roles:
    PlannerAgent      topic (+ memory) -> ResearchPlan
    ResearcherAgent   sub-question     -> Finding            (uses web search)
    VerifierAgent     finding          -> VerificationReport (adversarial)
    EditorAgent       findings+checks  -> Critique           (drives the loop)
    SynthesizerAgent  everything       -> Markdown report

Every agent parses model output through :mod:`vibe_research.schemas`, so a
malformed or hallucinated response is caught at the boundary and either salvaged
or replaced with a safe fallback — the pipeline never trusts raw text.
"""

from __future__ import annotations

import asyncio
import json
import re

from .backends import Backend, extract_urls
from .schemas import (
    Critique,
    Finding,
    ResearchPlan,
    ResearchRecord,
    ValidationError,
    VerificationReport,
    aggregate_verifications,
    clean_str,
)


class Agent:
    """Common base: hold a backend and the model this role runs on."""

    role = "agent"

    def __init__(self, backend: Backend, model: str) -> None:
        self.backend = backend
        self.model = model


def _memory_context(prior: list[ResearchRecord] | None) -> str:
    """Render recalled memory into a short brief the planner can build on."""
    if not prior:
        return ""
    lines = []
    for rec in prior:
        pts = "; ".join(rec.key_points[:4]) or "(no distilled points)"
        lines.append(f"- On '{rec.topic}' (confidence {rec.confidence:.0%}): {pts}")
    return (
        "\n\nYou have researched adjacent topics before. Use this to AVOID "
        "re-asking what is already known and to target genuinely new ground:\n"
        + "\n".join(lines)
    )


class PlannerAgent(Agent):
    """Research lead: decompose a topic into non-overlapping sub-questions."""

    role = "planner"

    async def plan(
        self,
        topic: str,
        n: int,
        *,
        prior: list[ResearchRecord] | None = None,
    ) -> ResearchPlan:
        prompt = (
            f"You are a research lead. Break this topic into {n} focused, "
            "non-overlapping sub-questions that together give thorough coverage.\n\n"
            f"TOPIC: {topic}"
            + _memory_context(prior)
            + "\n\nReturn ONLY a JSON object: "
            '{"subquestions": [{"text": "...", "rationale": "..."}, ...]}. '
            "No prose, no markdown fences."
        )
        text = ""
        try:
            text, _ = await self.backend.complete(prompt, model=self.model)
        except Exception:
            text = ""  # backend failure -> fall through to the topic-only salvage
        try:
            return ResearchPlan.parse(topic, text, hard_cap=n + 3)
        except ValidationError:
            # Last-ditch salvage: treat non-empty lines as sub-questions, or fall
            # back to the raw topic so the run can still proceed.
            lines = [ln.strip(" \t-•*0123456789.)") for ln in (text or "").splitlines()]
            lines = [ln for ln in lines if len(ln) > 8][: max(1, n)]
            payload = json.dumps({"subquestions": lines or [topic]})
            return ResearchPlan.parse(topic, payload, hard_cap=n + 3)

    async def replan(
        self,
        topic: str,
        gaps: list[str],
        answered: list[str],
        n: int,
    ) -> ResearchPlan:
        """Turn the editor's identified gaps into fresh, new sub-questions."""
        answered_block = "\n".join(f"- {q}" for q in answered) or "(none yet)"
        gaps_block = "\n".join(f"- {g}" for g in gaps) or "(none)"
        prompt = (
            f"You are a research lead refining coverage of: {topic}\n\n"
            "ALREADY ANSWERED (do NOT repeat these):\n"
            f"{answered_block}\n\n"
            "GAPS AND WEAKNESSES the fact-checker/editor flagged:\n"
            f"{gaps_block}\n\n"
            f"Propose up to {n} NEW, specific sub-questions that close those gaps "
            "without overlapping the answered ones. If the gaps are trivial or "
            'already covered, return {"subquestions": []}.\n\n'
            'Return ONLY JSON: {"subquestions": [{"text": "...", "rationale": "..."}]}.'
        )
        try:
            text, _ = await self.backend.complete(prompt, model=self.model)
            return ResearchPlan.parse(topic, text, hard_cap=n)
        except Exception:
            # Unreadable or failed re-plan -> no new questions; the loop ends cleanly.
            return ResearchPlan(topic=topic, subquestions=[])


class ResearcherAgent(Agent):
    """Field researcher: answer one sub-question with live web search."""

    role = "researcher"

    async def research(self, question: str, sem: asyncio.Semaphore) -> Finding:
        async with sem:
            try:
                text, sources = await self.backend.complete(
                    "Research this question using web search. Give a thorough, factual "
                    "answer grounded in what you find, and include the source URLs you "
                    "used. Prefer primary and authoritative sources. If sources conflict "
                    "or the evidence is thin, say so explicitly instead of guessing. End "
                    'your answer with a line "CONFIDENCE: 0.0-1.0" reflecting how well '
                    f"sourced your answer is.\n\nQUESTION: {question}",
                    model=self.model,
                    use_search=True,
                )
            except Exception as exc:
                # A transient backend failure (rate limit, timeout, network) on one
                # of several parallel threads must not sink the whole run — degrade
                # this sub-question to a zero-confidence placeholder and carry on.
                return Finding.build(
                    question,
                    f"Research could not be completed for this sub-question ({exc}).",
                    [],
                    confidence=0.0,
                )
            confidence = _extract_confidence(text)
            return Finding.build(question, text, sources, confidence=confidence)


def _extract_confidence(text: str, default: float = 0.6) -> float:
    """Pull a trailing 'CONFIDENCE: x' hint out of a researcher answer.

    Matches a whole number then range-checks it, so an out-of-range value like
    a percentage ('CONFIDENCE: 100') or an out-of-10 score falls back to the
    default rather than being silently truncated to a falsely-maxed 1.0.
    """
    match = re.search(r"confidence[:\s]+([0-9]+(?:\.[0-9]+)?)", text or "", re.IGNORECASE)
    if match:
        try:
            value = float(match.group(1))
        except ValueError:
            return default
        return value if 0.0 <= value <= 1.0 else default
    return default


class VerifierAgent(Agent):
    """Skeptical fact-checker: judge each claim in a finding against its sources.

    Prompted adversarially — its job is to *doubt*. Run several of these over the
    same finding and aggregate to get a debate/vote rather than one opinion.
    """

    role = "verifier"

    async def verify(self, topic: str, finding: Finding, lens: str = "") -> VerificationReport:
        lens_line = f"\nAdopt this reviewer lens: {lens}." if lens else ""
        prompt = (
            f"You are a skeptical fact-checker reviewing research on: {topic}{lens_line}\n\n"
            "For the finding below, extract its main factual claims and judge each "
            "one strictly against the cited sources. Default to doubt: if a claim "
            "is not clearly backed by a listed source, it is not 'supported'.\n\n"
            f"SUB-QUESTION: {finding.question}\n"
            f"ANSWER:\n{finding.answer}\n"
            f"SOURCES: {', '.join(finding.sources) or 'NONE'}\n\n"
            "Return ONLY JSON:\n"
            "{\n"
            '  "verdicts": [{"claim": "...", "status": "supported|partly-supported|'
            'unsupported|contradicted|uncertain", "confidence": 0.0-1.0, "note": "..."}],\n'
            '  "gaps": ["..."],\n'
            '  "contradictions": ["..."],\n'
            '  "overall_confidence": 0.0-1.0\n'
            "}"
        )
        try:
            text, _ = await self.backend.complete(prompt, model=self.model)
            return VerificationReport.parse(text)
        except ValidationError:
            # A verifier that returns garbage should not sink the finding; treat
            # it as a low-confidence abstention.
            return VerificationReport(overall_confidence=0.4, gaps=["fact-check unreadable"])
        except Exception:
            # Likewise a backend failure (rate limit/timeout) abstains rather than
            # crashing the whole verify round.
            return VerificationReport(overall_confidence=0.4, gaps=["fact-check failed"])

    async def debate(
        self,
        topic: str,
        finding: Finding,
        votes: int,
        lenses: list[str] | None = None,
    ) -> VerificationReport:
        """Run ``votes`` independent verifiers and aggregate their consensus."""
        votes = max(1, votes)
        lenses = lenses or ["source-fidelity", "logical-consistency", "recency-and-bias"]
        picks = [lenses[i % len(lenses)] if votes > 1 else "" for i in range(votes)]
        reports = await asyncio.gather(
            *(self.verify(topic, finding, lens=lens) for lens in picks)
        )
        return aggregate_verifications(list(reports))


class EditorAgent(Agent):
    """Managing editor: decide if the body of research is good enough yet.

    Its ``missing`` list is the loop's steering signal — those become the next
    round of sub-questions. Approving stops the loop.
    """

    role = "editor"

    async def critique(
        self,
        topic: str,
        findings: list[Finding],
        verifications: list[VerificationReport],
        *,
        threshold: float,
    ) -> Critique:
        blocks = []
        for finding, verification in zip(findings, verifications):
            weak = "; ".join(f"{v.claim} [{v.status}]" for v in verification.weak_claims) or "none"
            gaps = "; ".join(verification.gaps) or "none"
            blocks.append(
                f"### {finding.question}\n"
                f"confidence: {verification.overall_confidence:.2f} | "
                f"sources: {len(finding.sources)}\n"
                f"weak/failed claims: {weak}\n"
                f"open gaps: {gaps}"
            )
        dump = "\n\n".join(blocks)
        prompt = (
            f"You are the managing editor for a research report on: {topic}\n\n"
            "Below is every sub-question with its fact-check score, weak claims and "
            "open gaps. Decide whether coverage is thorough and well-supported "
            f"enough to publish (target confidence >= {threshold:.2f}).\n\n"
            f"{dump}\n\n"
            "Return ONLY JSON:\n"
            "{\n"
            '  "approved": true|false,\n'
            '  "quality": 0.0-1.0,\n'
            '  "missing": ["specific new sub-questions worth researching to close '
            'the most important gaps"],\n'
            '  "issues": ["problems the writer must address"],\n'
            '  "note": "one-line justification"\n'
            "}\n"
            "Approve only if there are no important gaps and overall support is strong."
        )
        try:
            text, _ = await self.backend.complete(prompt, model=self.model)
            return Critique.parse(text)
        except Exception:
            # If the editor is unreadable or unavailable, approve so the (already
            # bounded) loop ends cleanly instead of crashing on a transient error.
            return Critique(approved=True, quality=0.5, note="editor output unavailable")


class SynthesizerAgent(Agent):
    """Writer: turn validated findings + fact-checks into the final report."""

    role = "synthesizer"

    async def write(
        self,
        topic: str,
        findings: list[Finding],
        verifications: list[VerificationReport],
        critique: Critique,
        overall_confidence: float,
    ) -> str:
        blocks = []
        for finding, verification in zip(findings, verifications):
            flags = "; ".join(f"{v.claim} [{v.status}]" for v in verification.weak_claims)
            blocks.append(
                f"### {finding.question}\n{finding.answer}\n"
                f"SOURCES: {', '.join(finding.sources) or 'none'}\n"
                f"FACT-CHECK confidence: {verification.overall_confidence:.2f}"
                + (f"\nFLAGGED: {flags}" if flags else "")
            )
        dump = "\n\n".join(blocks)
        editor_notes = "; ".join(critique.issues) or "none"
        prompt = (
            f"Write a clear, well-structured research report in Markdown on:\n{topic}\n\n"
            "Use the fact-checked findings below. Rules:\n"
            "- Organise into logical sections with headings.\n"
            "- Keep source URLs next to the claims they support.\n"
            "- Do NOT restate claims the fact-checker flagged as unsupported or "
            "contradicted as if they were established; either drop them or clearly "
            "mark them as unverified.\n"
            "- Address the editor's issues.\n"
            f"- End with a section '## Confidence & Gaps' (overall confidence "
            f"{overall_confidence:.0%}) that honestly states what is well-established "
            "versus uncertain, and lists remaining gaps.\n"
            "- Do NOT invent facts or sources.\n\n"
            f"EDITOR ISSUES TO ADDRESS: {editor_notes}\n\n"
            f"FINDINGS:\n{dump}"
        )
        try:
            report, _ = await self.backend.complete(prompt, model=self.model)
            report = clean_str(report)
            if report:
                return report
        except Exception:
            pass
        # Writer failed/empty: assemble a plain report from the validated findings
        # so the run still yields something useful rather than crashing at the end.
        return self._fallback_report(topic, findings, verifications, overall_confidence)

    @staticmethod
    def _fallback_report(
        topic: str,
        findings: list[Finding],
        verifications: list[VerificationReport],
        overall_confidence: float,
    ) -> str:
        parts = [
            f"# {topic}",
            "",
            f"_Automated draft — the write-up step was unavailable, so this is "
            f"assembled directly from the findings. Overall confidence "
            f"{overall_confidence:.0%}._",
            "",
        ]
        gaps: list[str] = []
        for finding, verification in zip(findings, verifications):
            parts.append(f"## {finding.question}")
            parts.append(finding.answer or "_(no answer)_")
            if finding.sources:
                parts.append("")
                parts.append("Sources: " + ", ".join(finding.sources))
            parts.append("")
            gaps.extend(verification.gaps)
        parts.append("## Confidence & Gaps")
        parts.append("\n".join(f"- {g}" for g in dict.fromkeys(gaps)) or "No gaps recorded.")
        return "\n".join(parts)


def _safe_rewrite(original: str, rewritten: str, min_ratio: float = 0.7) -> bool:
    """True if a humanized rewrite is safe to keep over the original draft.

    Guards the two things the humanizer must never quietly lose:
    * citations — most of the original's source URLs must survive (the tool's
      whole 'fully-cited' promise), and
    * the honesty contract — if the draft had a 'Confidence & Gaps' section, the
      rewrite must keep one too.
    If either check fails the caller keeps the untouched draft.
    """
    original_urls = set(extract_urls(original))
    if original_urls:
        kept = original_urls & set(extract_urls(rewritten))
        if len(kept) / len(original_urls) < min_ratio:
            return False
    if "confidence & gaps" in original.lower() and "confidence & gaps" not in rewritten.lower():
        return False
    return True


class HumanizerAgent(Agent):
    """Final-pass editor: rewrite the report so it reads like a person wrote it.

    This is the last step in the research sequence. It changes only voice and
    flow — never the facts or the citations — and falls back to the untouched
    draft if the rewrite fails or strips too many source URLs.
    """

    role = "humanizer"

    async def humanize(self, topic: str, report: str) -> str:
        if not (report or "").strip():
            return report
        prompt = (
            "Rewrite the following research report so it reads like it was written "
            "by a knowledgeable human writer, not generated by an AI.\n\n"
            "KEEP ALL SUBSTANCE INTACT:\n"
            "- Do not add, remove, or change any fact, figure, or claim.\n"
            "- Keep every source URL exactly where it supports its claim.\n"
            "- Keep the section headings and the '## Confidence & Gaps' section.\n\n"
            "CHANGE ONLY THE VOICE AND FLOW:\n"
            "- Vary sentence length and rhythm; avoid uniform, list-like paragraphs.\n"
            "- Cut AI tells: 'it is important to note', 'moreover'/'furthermore' "
            "chains, 'delve', 'tapestry', 'in conclusion', hollow hedging, and "
            "over-signposting.\n"
            "- Use plain, direct, mostly active voice; let some sentences be short.\n"
            "- Read like an expert explaining to a smart colleague: confident, "
            "specific, and honest about uncertainty.\n\n"
            "Output ONLY the rewritten report in Markdown — no preamble, no "
            "'here is the rewrite'.\n\n"
            f"TOPIC: {topic}\n\nREPORT:\n{report}"
        )
        try:
            text, _ = await self.backend.complete(prompt, model=self.model)
            text = clean_str(text)
            if text and _safe_rewrite(report, text):
                return text
        except Exception:
            pass
        return report  # rewrite failed or dropped citations/section -> keep the draft
