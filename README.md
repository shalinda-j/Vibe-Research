# vibe-research

[![CI](https://github.com/shalinda-j/Vibe-Research/actions/workflows/ci.yml/badge.svg)](https://github.com/shalinda-j/Vibe-Research/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

An autonomous, fully-cited **multi-agent research crew** that lives in your terminal.
Give it a topic; a planner splits it into sub-questions, parallel researchers answer
each with live web search, a panel of adversarial fact-checkers votes on every claim,
an editor decides whether coverage is good enough — and if not, the crew **re-plans
and researches the gaps itself** before writing a cited Markdown report. Hands-off.

It also **remembers**: each run is distilled to long-term memory so later, related
topics build on what it already learned instead of starting cold.

```
vibe-research "impact of EPF rate changes on SME payroll in Sri Lanka"
```

A split-screen TUI shows the agent working on the left and the report building on
the right. Reports are saved to disk automatically.

---

## Honest expectations (read this)

This tool is engineered to be **accurate and fully cited**, not "100% correct". No
LLM-based tool can promise zero mistakes — they can hallucinate. What `vibe-research`
does instead:

- grounds every claim in a real source (with URLs),
- runs a dedicated **verify** pass that re-checks claims and flags weak/conflicting ones,
- ends every report with a **"Confidence & Gaps"** section that says where it's unsure.

For anything high-stakes (compliance, legal, financial numbers), read that section and
sanity-check the sources. The tool runs fully autonomously by default; "autonomous"
isn't the same as "safe to use unread for things that bite you."

---

## Engines, one tool

`vibe-research` runs on any of several backends. It **auto-detects** an Anthropic
engine, or you can force one with `--mode`.

| Mode | Engine | Billing | Best for |
| --- | --- | --- | --- |
| `api` | Anthropic Messages API | pay-per-token (Console) | products, multi-user, always-on |
| `subscription` | Claude Agent SDK | draws from your Claude subscription | your own / internal use |
| `openai` | OpenAI Responses API | pay-per-token (OpenAI) | GPT models / web search |
| `gemini` | Google Gemini (OpenAI-compat) | pay-per-token (Google) | Gemini models |
| `glm` | Zhipu GLM (OpenAI-compat) | pay-per-token (Zhipu) | GLM models / built-in web search |
| `kimi` | Moonshot Kimi (OpenAI-compat) | pay-per-token (Moonshot) | Kimi models |

The `gemini`/`glm`/`kimi` engines all speak the OpenAI API, so they need the same
`[openai]` extra and their own API key (`GEMINI_API_KEY` / `GLM_API_KEY` /
`KIMI_API_KEY`). Claude-named model defaults auto-map to each provider's models;
override with `--planner-model`/`--worker-model`. Live web search is built in for
`api`, `openai`, and `glm`; the others answer from model knowledge (the
fact-checker scores sourcing accordingly).

> **Subscription mode caveat.** Anthropic does not permit third-party apps to offer
> claude.ai login to *other* users without prior approval, and the subscription-billing
> path for the Agent SDK is something Anthropic has said it may change. Use subscription
> mode for **your own** usage. For a customer-facing product, use `api` mode.

> **OpenAI has no subscription API.** Unlike Claude's Agent SDK path, OpenAI API usage
> is always metered per token against your OpenAI account — a ChatGPT Plus/Pro plan does
> not grant programmatic access. Set `OPENAI_API_KEY` and run with `--mode openai`
> (`pip install "vibe-research[openai]"`). Claude-named model defaults are auto-mapped to
> `gpt-4o` / `gpt-4o-mini`; override with `--planner-model gpt-5` etc.

---

## Install

Like other terminal tools, `pipx` is the cleanest install (isolated, on your PATH):

```bash
pipx install .
# or, from the project directory during development:
pip install -e ".[dev]"
```

That gives you **API mode** out of the box. For **subscription mode**, also install the
Agent SDK extra and set up the Claude Code login (see below):

```bash
pipx install ".[subscription]"
```

For **PDF export** (`--pdf` / `Ctrl+P`), add the `pdf` extra — pure-Python, no
system libraries, and it uses a system Unicode font so accents/arrows render:

```bash
pipx install ".[pdf]"        # or: pip install fpdf2
```

Check everything at any time:

```bash
vibe-research doctor
```

### Set up API mode

```bash
export ANTHROPIC_API_KEY=sk-ant-...     # get one at https://console.anthropic.com
vibe-research "your topic"
```

### Set up subscription mode

```bash
npm install -g @anthropic-ai/claude-code   # the engine the Agent SDK wraps
claude                                      # then type /login and sign in (Pro/Max)
pip install claude-agent-sdk
unset ANTHROPIC_API_KEY                      # IMPORTANT: a stray key bills you per token
vibe-research --mode subscription "your topic"
```

After a run, confirm usage showed up on your **Claude.ai** account (not the API console)
to be sure the subscription path is wired.

---

## Usage

```bash
vibe-research "your topic"                 # TUI (default)
vibe-research run "your topic" --no-tui    # headless: prints progress + saves report
vibe-research --mode subscription "topic"  # force subscription engine
vibe-research --mode openai "topic"        # use OpenAI (needs OPENAI_API_KEY)
vibe-research --mode gemini "topic"        # Gemini (GEMINI_API_KEY) — also glm, kimi
vibe-research run "topic" --parallel 3 --subquestions 6

# autonomy knobs
vibe-research run "topic" --iterations 3   # up to 3 self-refining (gap-filling) rounds
vibe-research run "topic" --votes 3        # 3 adversarial fact-checkers per finding
vibe-research run "topic" --quality 0.85   # keep refining until 85% confidence
vibe-research run "topic" --no-debate      # single fact-check instead of a vote
vibe-research run "topic" --no-memory      # don't recall or persist memory
vibe-research run "topic" --no-humanize    # skip the human-voice rewrite (raw draft)

# sourcing & depth
vibe-research run "topic" --depth deep          # quick | standard | deep preset
vibe-research run "topic" --since 2022          # prefer recent sources
vibe-research run "topic" --only-domains gov,edu     # restrict to trusted domains
vibe-research run "topic" --block-domains reddit.com # drop specific domains
vibe-research run "topic" --citations plain     # plain source list (default: ranked)

# per-stage models
vibe-research run "topic" --writer-model claude-opus-4-8 --verifier-model claude-sonnet-4-6

# length, style & visuals
vibe-research run "topic" --words 1500          # target length (or --pages 3)
vibe-research run "topic" --style essay         # report | essay | brief
vibe-research run "topic" --no-charts --no-diagrams   # opt out of visuals

# output & UX
vibe-research run "topic" --pdf --html --json --docx   # PDF, HTML, JSON sidecar, Word doc
vibe-research run "topic" --open                # open the report when it's done
vibe-research run "topic" --no-tui --quiet      # print only the saved path(s)
vibe-research run "topic" --no-tui --verbose    # print fact-check + editor detail
vibe-research run "topic" --debug               # write a JSONL trace of every model call

# reliability (retries/timeout/throttle per model call)
vibe-research run "topic" --retries 5 --timeout 240 --concurrency 6

vibe-research doctor                        # environment / deps / pipeline check
vibe-research history                       # list past reports
vibe-research memory                        # list long-term memory records
vibe-research memory --clear                # wipe long-term memory
vibe-research config                        # show config
vibe-research config --set max_iterations=3
vibe-research config --set enable_memory=false
vibe-research config --set export_pdf=true  # always export PDF too
vibe-research --version
```

In the **TUI**, `Ctrl+P` exports the current report to a PDF (saved next to the
Markdown file); `Ctrl+S` copies it to the clipboard.

### Config

Stored at `~/.config/vibe-research/config.json`:

| Key | Default | Meaning |
| --- | --- | --- |
| `mode` | `auto` | `auto`, `api`, `subscription`, or `openai` |
| `planner_model` | `claude-opus-4-8` | planning, fact-check, editing, write-up |
| `worker_model` | `claude-sonnet-4-6` | the many web-search calls |
| `max_parallel` | `2` | concurrent research threads |
| `subquestions` | `5` | how many sub-questions to research |
| `max_iterations` | `2` | self-refining rounds (re-plan + research gaps) |
| `verifier_votes` | `2` | adversarial fact-checkers per finding |
| `quality_threshold` | `0.75` | editor confidence needed to stop refining |
| `enable_debate` | `true` | multi-verifier voting vs. a single fact-check |
| `enable_memory` | `true` | recall from / persist to long-term memory |
| `humanize` | `true` | final pass: rewrite the report in a natural human voice |
| `citations` | `ranked` | `ranked` (by source credibility) or `plain` list |
| `since_year` | `0` | prefer sources from this year onward (`0` = off) |
| `only_domains` | `""` | comma-sep domain substrings to keep (e.g. `gov,edu`) |
| `block_domains` | `""` | comma-sep domain substrings to drop (e.g. `reddit.com`) |
| `verifier_model` / `writer_model` / `humanizer_model` | `""` | per-stage model overrides (empty → planner model) |
| `prose_style` | `report` | `report`, `essay`, or `brief` writing style |
| `words` | `0` | target word count (`0` = model decides) |
| `enable_charts` / `enable_diagrams` / `enable_figures` | `true` | allow data charts / mermaid diagrams / figures |
| `export_pdf` | `false` | also write a PDF beside every saved report |
| `export_html` | `false` | also write a styled HTML page beside every report |
| `export_json` | `false` | also write a structured JSON sidecar (findings + verdicts) |
| `export_docx` | `false` | also write a Word `.docx` (needs the `[docx]` extra) |
| `debug` | `false` | write a JSONL trace of every model call |
| `open_after` | `false` | open the report when a run finishes |
| `max_retries` | `3` | exponential-backoff retries per model call (`0` = none) |
| `call_timeout` | `180` | per-call timeout in seconds (`0` = none) |
| `max_concurrency` | `4` | cap on simultaneous model calls (API-pressure smoothing) |
| `reports_dir` | (default) | where reports are saved |
| `memory_dir` | (default) | where long-term memory is stored |

Reports default to `~/.local/share/vibe-research/reports/` and memory to
`~/.local/share/vibe-research/memory/`.

Every `.md` report opens with a metadata line (date · mode · confidence · sources ·
sub-questions). With `--json` you also get a machine-readable sidecar containing
every finding, its confidence, the fact-check verdicts, and the run's token usage.

### Reliability

Model calls are wrapped so a flaky network doesn't sink a multi-minute run: each
call is **retried with exponential backoff** on transient errors (429 / 5xx /
timeout / connection), bounded by a **per-call timeout**, and the whole run is
**throttled** to `max_concurrency` simultaneous calls so it doesn't hammer the API.
Anything that still fails degrades gracefully — a dead research thread becomes a
zero-confidence finding, a failed fact-check abstains — rather than crashing.
`Ctrl+C` stops a headless run cleanly.

---

## How it works

A crew of single-purpose agents, coordinated in a self-refining loop:

```
              ┌─────────── memory (recall related past runs) ───────────┐
              ▼                                                          │
topic ─▶ PLANNER ─▶ RESEARCHERS (parallel, web search) ─▶ VERIFIERS (vote) ─▶ EDITOR
              ▲                                                              │
              └──────────── gaps? re-plan & research more ◀─────────────────┘
                                        │ approved / confidence met / out of rounds
                                        ▼
                        SYNTHESIZER ─▶ HUMANIZER ─▶ validated report ─▶ remember
```

1. **Planner** — splits the topic into focused, non-overlapping sub-questions,
   seeded with anything relevant recalled from long-term memory.
2. **Researchers** — answer each sub-question with live web search + citations, in
   parallel. Every answer is parsed and **validated** into a typed structure.
3. **Verifiers** — several *adversarial* fact-checkers judge each claim against its
   sources with different lenses; their votes are aggregated into one confidence.
4. **Editor** — decides whether coverage is thorough and well-supported enough. If
   not, it names the gaps, the planner turns them into **new** sub-questions, and
   the crew researches them — looping until it's confident or runs out of rounds.
5. **Synthesizer** — writes a cited report, dropping or flagging claims the
   fact-checkers rejected, and ends with an honest "Confidence & Gaps" section.
   Conflicting evidence is surfaced in a **Disagreements** section, and sources
   are listed **ranked by credibility** (primary/authoritative → news → blog).
6. **Humanizer** — a final pass that rewrites the draft in a natural human voice
   (varied rhythm, no AI tells), changing *only* voice and flow. It never alters
   a fact and is guarded so it can't drop citations — if a rewrite loses too many
   source URLs, the original draft is kept.
7. **Memory** — the run is distilled to disk so future related topics build on it.

Every value that crosses an agent boundary is run through a strict, stdlib-only
**data-validation layer** (`schemas.py`): malformed or hallucinated model output is
caught at the boundary and either salvaged or safely rejected — the pipeline never
trusts raw text.

---

## Development

```bash
make dev      # editable install with dev + subscription extras
make test     # run the offline test-suite (no network needed)
make doctor   # environment check
```

The test-suite runs the full pipeline against a fake backend, so it validates the
orchestration logic without any API calls or network access.

---

## Roadmap

Bigger features that need live external services or are larger projects (open to
contributions):

- **Local backend** — **Ollama** for fully offline / free runs (OpenAI, Gemini, GLM, Kimi supported).
- **Local document RAG** — research over your own PDFs/notes.
- **MCP server mode** — expose vibe-research as a tool to other agents.
- **Recursive / multi-hop research** — drill deeper into a single finding.
- **Source archival** — snapshot cited pages against link rot.
- **Streaming TUI**, **scheduled watch mode**, **Obsidian/Notion sync**.

See `CHANGELOG.md` for what's already shipped.

---

## License

MIT. See `LICENSE`.
