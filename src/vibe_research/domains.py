"""Domain specialisation for vibe-research.

A :class:`Domain` bundles the extra, field-specific instructions that steer the
crew for a particular kind of research — how the planner decomposes the topic,
what the researcher prioritises and cites, the lens the fact-checkers apply, the
structure and rigour the writer uses, and a safety disclaimer bolted onto the
report. It's the difference between a generic web summary and a report that reads
like it was written by someone who knows the field.

The first specialised domain is **medical / biomedical** (drugs, chemistry, new
therapies, clinical evidence). ``general`` is the empty default, so existing
behaviour is unchanged unless a domain is chosen.

stdlib-only (just a dataclass + strings) so the offline test-suite covers it and
nothing here pulls in a heavy dependency.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Domain:
    """A field-specific instruction bundle appended to the crew's prompts."""

    name: str
    label: str = ""
    planner: str = ""       # appended to the planner's decomposition prompt
    researcher: str = ""    # appended to each researcher's sourcing/answer prompt
    verifier: str = ""      # appended to the fact-checker's prompt
    writer: str = ""        # appended to the synthesiser's write-up prompt
    disclaimer: str = ""    # a safety notice surfaced at the top of the report
    # Extra adversarial fact-check lenses used by the verifier debate (they replace
    # the generic lenses so the votes probe field-specific failure modes).
    lenses: tuple[str, ...] = ()
    # Soft sourcing preference (host substrings) folded into research guidance.
    preferred_domains: tuple[str, ...] = field(default_factory=tuple)


GENERAL = Domain(name="general")


MEDICAL = Domain(
    name="medical",
    label="Medical / biomedical research",
    planner=(
        "\n\nThis is a MEDICAL / BIOMEDICAL topic (drugs, chemicals, therapies, "
        "clinical science). Decompose it along the axes a clinician or pharmacologist "
        "would need: (1) mechanism of action / pharmacology / relevant chemistry, "
        "(2) clinical efficacy and the trial evidence for it, (3) safety, adverse "
        "effects and pharmacovigilance signals, (4) dosing, administration and "
        "pharmacokinetics, (5) contraindications, drug–drug and drug–disease "
        "interactions, (6) regulatory status (FDA/EMA/MHRA approval, indications, "
        "black-box warnings), and (7) comparative effectiveness vs standard of care. "
        "Skip axes that don't apply to this specific topic."
    ),
    researcher=(
        "Prioritise high-quality biomedical evidence, most authoritative first: "
        "systematic reviews and meta-analyses (Cochrane), randomised controlled "
        "trials, then cohort/case-control studies, indexed via PubMed/PMC; registered "
        "trials on ClinicalTrials.gov; regulatory sources (FDA/EMA drug labels, "
        "approval documents); and reference works (DrugBank, MedlinePlus, StatPearls, "
        "UpToDate). For every factual claim, state the STUDY TYPE (meta-analysis, RCT, "
        "cohort, case series, in-vitro, animal), the sample size, and whether it is "
        "HUMAN or preclinical (animal/in-vitro) evidence — never present preclinical "
        "or mechanistic findings as established clinical fact. Give effect sizes with "
        "confidence intervals and absolute (not just relative) risks where reported. "
        "Use precise terms (drug/INN names, doses with units, routes). Explicitly flag "
        "anything off-label, experimental, based on weak/low evidence, contested, or "
        "from a retracted or predatory source. If the evidence is thin, say so."
    ),
    verifier=(
        "Apply clinical-evidence standards. GRADE each key claim by the strength of "
        "the study design behind it (systematic review/RCT > cohort > case report > "
        "in-vitro/animal > expert opinion). Treat a claim as UNSUPPORTED if it "
        "over-generalises from animal/in-vitro or mechanistic data to human clinical "
        "effect, rests on a single small or unblinded study, relies on surrogate "
        "endpoints presented as hard outcomes, ignores conflicts of interest or "
        "industry funding, or cites a non-peer-reviewed, predatory or retracted "
        "source. Be especially strict on safety, dosing and interaction claims — these "
        "carry patient risk."
    ),
    writer=(
        "Write as a rigorous clinical/biomedical evidence review. Organise into "
        "sections such as: Overview, Mechanism of Action / Chemistry, Clinical "
        "Evidence (efficacy), Safety & Adverse Effects, Dosing & Pharmacokinetics, "
        "Contraindications & Interactions, Regulatory Status, and Comparative "
        "Effectiveness — include only those the evidence supports. Tag key claims with "
        "an evidence level (e.g. 'systematic review', 'RCT', 'observational', "
        "'preclinical', 'expert opinion') and keep the human/preclinical distinction "
        "explicit. State doses, units and routes precisely; never invent a number. "
        "Do NOT phrase anything as a personalised recommendation, prescription, or "
        "instruction to a patient — describe the evidence, not what a reader should do."
    ),
    disclaimer=(
        "⚕️ **Medical disclaimer** — This is an AI-generated summary of published "
        "research for informational and educational purposes only. It is **not medical "
        "advice, diagnosis, or treatment**, and may be incomplete, outdated, or "
        "mistaken. Drug doses, interactions and contraindications must be verified "
        "against current prescribing information. Consult a qualified healthcare "
        "professional and the primary sources before making any clinical decision."
    ),
    lenses=(
        "clinical-evidence-grade",      # is the study design strong enough?
        "pharmacovigilance-safety",     # adverse effects, interactions, dosing risk
        "preclinical-vs-human",         # over-generalisation from animal/in-vitro
    ),
    preferred_domains=(
        "pubmed", "ncbi.nlm.nih.gov", "clinicaltrials.gov", "cochrane",
        "fda.gov", "ema.europa.eu", "who.int", "drugbank", "medlineplus.gov",
        "nejm.org", "thelancet.com", "jamanetwork.com", "bmj.com",
    ),
)


DOMAINS: dict[str, Domain] = {d.name: d for d in (GENERAL, MEDICAL)}

# Names a user can pass to --domain / config, excluding the no-op default.
SPECIALISED = tuple(name for name in DOMAINS if name != "general")


def get_domain(name: str | None) -> Domain:
    """Resolve a domain name (case-insensitive) to its :class:`Domain`.

    Unknown or empty names fall back to :data:`GENERAL`, so a bad value can never
    crash a run — it just means "no specialisation".
    """
    return DOMAINS.get((name or "general").strip().lower(), GENERAL)
