"""The autonomous, self-refining research orchestrator.

This is the coordinator that turns a crew of single-purpose agents
(:mod:`vibe_research.agents`) into a research loop:

    recall memory
        │
        ▼
    PLAN ─▶ RESEARCH (parallel) ─▶ VERIFY (debate/vote) ─▶ CRITIQUE (editor)
        ▲                                                        │
        └──────────── gaps? re-plan & research more ◀────────────┘  (bounded)
                                    │ approved / confidence met / out of rounds
                                    ▼
                         WRITE ─▶ validated report ─▶ remember

Backend- and UI-agnostic: progress flows through an ``on_event`` callback so the
same orchestrator drives the TUI and headless mode. Legacy event kinds
(``start``/``stage``/``plan``/``finding``/``verify``/``done``) are preserved so
older consumers keep working; new kinds (``memory``/``debate``/``critique``/
``iteration``) layer the multi-agent detail on top.

Honesty by design: every claim is fact-checked by multiple adversarial verifiers,
the editor gates on a confidence threshold, and the report ends with an explicit
"Confidence & Gaps" section. No LLM guarantees zero mistakes — this makes the
uncertainty visible instead of hiding it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Callable

from .agents import (
    EditorAgent,
    HumanizerAgent,
    PlannerAgent,
    ResearcherAgent,
    SynthesizerAgent,
    VerifierAgent,
)
from .backends import Backend
from .enrich import (
    credibility_summary,
    disagreements_section,
    filter_sources,
    rank_sources,
    sources_section,
)
from .schemas import (
    Critique,
    Finding,
    ResearchPlan,
    ResearchRecord,
    VerificationReport,
)

EventCallback = Callable[[str, dict], None]


def _noop(kind: str, data: dict) -> None:
    return None


def run_kwargs_from_config(cfg) -> dict:
    """Translate a :class:`~vibe_research.config.Config` into ``run_pipeline`` kwargs.

    Constructs the long-term :class:`~vibe_research.memory.Memory` when enabled so
    the CLI and TUI wire the pipeline up identically. Imported lazily to keep this
    module free of a hard dependency on config/memory at import time.
    """
    from .memory import Memory

    memory = Memory(cfg.resolved_memory_dir()) if cfg.enable_memory else None

    only = [d.strip() for d in (cfg.only_domains or "").split(",") if d.strip()]
    block = [d.strip() for d in (cfg.block_domains or "").split(",") if d.strip()]
    guide: list[str] = []
    if cfg.since_year:
        guide.append(f"Strongly prefer sources published in {cfg.since_year} or later.")
    if only:
        guide.append("Restrict to these domains where possible: " + ", ".join(only) + ".")
    if block:
        guide.append("Avoid these domains: " + ", ".join(block) + ".")

    return dict(
        planner_model=cfg.planner_model,
        worker_model=cfg.worker_model,
        subquestions=cfg.subquestions,
        max_parallel=cfg.max_parallel,
        max_iterations=cfg.max_iterations,
        verifier_votes=cfg.verifier_votes,
        quality_threshold=cfg.quality_threshold,
        enable_debate=cfg.enable_debate,
        enable_memory=cfg.enable_memory,
        humanize=cfg.humanize,
        verifier_model=cfg.verifier_model or None,
        writer_model=cfg.writer_model or None,
        humanizer_model=cfg.humanizer_model or None,
        research_guidance=" ".join(guide),
        only_domains=only,
        block_domains=block,
        citations=cfg.citations,
        memory=memory,
    )


# ------------------------------------------------------------------- helpers


def _overall_confidence(verifications: list[VerificationReport]) -> float:
    if not verifications:
        return 0.5
    return round(sum(v.overall_confidence for v in verifications) / len(verifications), 3)


def _review_text(findings: list[Finding], verifications: list[VerificationReport]) -> str:
    """A human-readable fact-check summary for the legacy ``verify`` event/TUI."""
    lines: list[str] = []
    for finding, verification in zip(findings, verifications):
        lines.append(
            f"• {finding.question[:70]}  "
            f"(confidence {verification.overall_confidence:.0%}, "
            f"{len(finding.sources)} sources)"
        )
        for verdict in verification.weak_claims:
            lines.append(f"    ! {verdict.status}: {verdict.claim[:80]}")
        for gap in verification.gaps[:3]:
            lines.append(f"    gap: {gap[:80]}")
    return "\n".join(lines) or "(no fact-check notes)"


def _first_point(answer: str, limit: int = 160) -> str:
    """Distil a finding's answer to a short key point for long-term memory."""
    for line in (answer or "").splitlines():
        line = line.strip(" \t-•*#")
        if len(line) > 20 and not line.lower().startswith("confidence"):
            return line[:limit]
    return (answer or "").strip()[:limit]


def _to_record(
    topic: str,
    plan: ResearchPlan,
    findings: list[Finding],
    verifications: list[VerificationReport],
    overall: float,
    report_path: str = "",
) -> ResearchRecord:
    sources: list[str] = []
    for finding in findings:
        for src in finding.sources:
            if src not in sources:
                sources.append(src)
    key_points = [_first_point(f.answer) for f in findings if f.answer]
    return ResearchRecord(
        topic=topic,
        created=datetime.now().isoformat(timespec="seconds"),
        subquestions=[f.question for f in findings] or plan.questions,
        key_points=[p for p in key_points if p],
        sources=sources,
        confidence=overall,
        report_path=report_path,
    )


def _all_sources(findings: list[Finding]) -> list[str]:
    seen: list[str] = []
    for finding in findings:
        for src in finding.sources:
            if src not in seen:
                seen.append(src)
    return seen


def _append_sources(report: str, findings: list[Finding], citations: str = "ranked") -> str:
    all_sources = _all_sources(findings)
    if not all_sources:
        return report
    low = report.lower()
    # Don't append a second list if the write-up already produced one under any
    # of the common reference headings.
    if any(h in low for h in (
        "## sources", "## all sources", "## references", "## bibliography", "## works cited",
    )):
        return report
    if citations == "plain":
        return report + "\n\n## All sources\n" + "\n".join(f"- {s}" for s in all_sources)
    return report + "\n\n" + sources_section(all_sources)


def _append_disagreements(report: str, verifications: list[VerificationReport]) -> str:
    contradictions: list[str] = []
    for verification in verifications:
        contradictions.extend(verification.contradictions)
    section = disagreements_section(contradictions)
    if section and "## disagreements" not in report.lower():
        report += "\n\n" + section
    return report


# ---------------------------------------------------------------- the loop


async def run_pipeline(
    backend: Backend,
    topic: str,
    *,
    planner_model: str,
    worker_model: str,
    subquestions: int,
    max_parallel: int,
    max_iterations: int = 2,
    verifier_votes: int = 2,
    quality_threshold: float = 0.75,
    enable_debate: bool = True,
    enable_memory: bool = False,
    humanize: bool = True,
    verifier_model: str | None = None,
    writer_model: str | None = None,
    humanizer_model: str | None = None,
    research_guidance: str = "",
    only_domains: list[str] | None = None,
    block_domains: list[str] | None = None,
    citations: str = "ranked",
    memory=None,
    on_event: EventCallback = _noop,
) -> str:
    """Run the full multi-agent research loop and return the Markdown report.

    The signature is backward-compatible with the original linear pipeline; the
    autonomy knobs (``max_iterations``, ``verifier_votes``, ``quality_threshold``,
    ``enable_debate``, ``enable_memory``, ``memory``) all have safe defaults.
    """
    on_event("start", {"topic": topic})

    planner = PlannerAgent(backend, planner_model)
    researcher = ResearcherAgent(backend, worker_model, guidance=research_guidance)
    verifier = VerifierAgent(backend, verifier_model or planner_model)
    editor = EditorAgent(backend, planner_model)
    writer = SynthesizerAgent(backend, writer_model or planner_model)

    sem = asyncio.Semaphore(max(1, max_parallel))
    findings: list[Finding] = []
    verifications: list[VerificationReport] = []

    async def research_batch(questions: list[str]) -> None:
        async def one(question: str) -> Finding:
            finding = await researcher.research(question, sem)
            if only_domains or block_domains:
                finding.sources = filter_sources(finding.sources, only_domains, block_domains)
            on_event("finding", {"question": finding.question, "n_sources": len(finding.sources)})
            return finding

        # gather preserves submission order, so report sections stay in plan order
        # while the per-thread events still fire as each one finishes.
        findings.extend(await asyncio.gather(*(one(q) for q in questions)))

    # --- memory recall: seed the planner with adjacent prior research ---------
    prior = []
    if enable_memory and memory is not None:
        try:
            prior = memory.related(topic, limit=3)
        except Exception:
            prior = []
        if prior:
            on_event("memory", {"recalled": len(prior), "topics": [r.topic for r in prior]})

    # --- plan -----------------------------------------------------------------
    on_event("stage", {"stage": "plan", "msg": "Planning sub-questions"})
    plan = await planner.plan(topic, subquestions, prior=prior)
    on_event("plan", {"questions": plan.questions})

    # --- initial research -----------------------------------------------------
    on_event("stage", {"stage": "research", "msg": f"Researching {len(plan.questions)} threads"})
    await research_batch(plan.questions)

    # --- verify / critique / (re-plan) loop -----------------------------------
    critique: Critique | None = None
    rounds = max(1, max_iterations)
    votes = verifier_votes if enable_debate else 1
    for round_i in range(1, rounds + 1):
        pending = findings[len(verifications):]
        on_event("stage", {"stage": "verify", "msg": f"Fact-checking {len(pending)} findings"})
        results = await asyncio.gather(
            *(verifier.debate(topic, finding, votes) for finding in pending)
        )
        for finding, verification in zip(pending, results):
            finding.confidence = verification.overall_confidence
            verifications.append(verification)
            on_event(
                "debate",
                {
                    "question": finding.question,
                    "confidence": verification.overall_confidence,
                    "votes": votes,
                },
            )

        on_event("verify", {"review": _review_text(findings, verifications)})

        on_event("stage", {"stage": "critique", "msg": "Editor reviewing coverage"})
        critique = await editor.critique(
            topic, findings, verifications, threshold=quality_threshold
        )
        overall = _overall_confidence(verifications)
        on_event(
            "critique",
            {
                "approved": critique.approved,
                "quality": critique.quality,
                "missing": critique.missing,
                "note": critique.note,
                "confidence": overall,
                "round": round_i,
            },
        )

        # Stop when good enough, out of gaps, or out of rounds.
        if round_i >= rounds:
            break
        if critique.approved and overall >= quality_threshold:
            break
        if not critique.missing:
            break

        answered = [f.question for f in findings]
        answered_norm = {q.strip().lower() for q in answered}
        on_event(
            "iteration",
            {
                "round": round_i + 1,
                "max": rounds,
                "reason": critique.note or "closing gaps",
                "gaps": critique.missing,
            },
        )
        replan = await planner.replan(topic, critique.missing, answered, subquestions)
        # Normalise (case/whitespace) when de-duping across rounds, matching how
        # ResearchPlan de-dups within a single plan, so a near-repeat isn't re-run.
        gap_questions = [
            q for q in replan.questions if q.strip().lower() not in answered_norm
        ]
        if not gap_questions:
            break
        on_event(
            "stage",
            {"stage": "research", "msg": f"Researching {len(gap_questions)} gap threads"},
        )
        await research_batch(gap_questions)

    # --- write ----------------------------------------------------------------
    on_event("stage", {"stage": "write", "msg": "Writing report"})
    overall = _overall_confidence(verifications)
    report = await writer.write(
        topic, findings, verifications, critique or Critique(approved=True), overall
    )

    # --- humanize -------------------------------------------------------------
    # Final pass: make the write-up read like a person wrote it (voice/flow only,
    # facts and citations preserved). Runs before sources are appended so the
    # rewrite can't touch the canonical "All sources" list.
    if humanize:
        on_event("stage", {"stage": "humanize", "msg": "Humanizing the write-up"})
        report = await HumanizerAgent(backend, humanizer_model or planner_model).humanize(topic, report)

    # Surface where sources conflict, then a credibility-ranked reference list.
    report = _append_disagreements(report, verifications)
    report = _append_sources(report, findings, citations)

    # --- remember -------------------------------------------------------------
    if enable_memory and memory is not None:
        try:
            memory.remember(_to_record(topic, plan, findings, verifications, overall))
            on_event("memory", {"saved": True, "topic": topic})
        except Exception:
            pass

    on_event("done", {
        "report": report,
        "confidence": overall,
        "result": _build_result(topic, backend, findings, verifications, overall),
    })
    return report


def _build_result(topic, backend, findings, verifications, overall) -> dict:
    """A structured, JSON-serialisable summary of the run for metadata + sidecar."""
    sources: list[str] = []
    for finding in findings:
        for src in finding.sources:
            if src not in sources:
                sources.append(src)
    try:
        usage = backend.usage()
    except Exception:
        usage = {}
    contradictions: list[str] = []
    for v in verifications:
        contradictions.extend(v.contradictions)
    return {
        "topic": topic,
        "mode": getattr(backend, "name", "?"),
        "overall_confidence": overall,
        "credibility": credibility_summary(sources),
        "sources_ranked": rank_sources(sources),
        "disagreements": list(dict.fromkeys(c.strip() for c in contradictions if c.strip())),
        "subquestions": [f.question for f in findings],
        "findings": [
            {
                "question": f.question,
                "confidence": f.confidence,
                "n_sources": len(f.sources),
                "sources": f.sources,
            }
            for f in findings
        ],
        "verifications": [
            {
                "overall_confidence": v.overall_confidence,
                "gaps": v.gaps,
                "flagged": [{"claim": vd.claim, "status": vd.status} for vd in v.weak_claims],
            }
            for v in verifications
        ],
        "sources": sources,
        "usage": usage,
    }
