# Changelog

All notable changes to **vibe-research** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[semantic versioning](https://semver.org/).

## [0.7.0]

### Added
- **Local & free engine** — `--mode ollama` runs the whole pipeline on a local
  [Ollama](https://ollama.com) server: no API key, fully offline, zero cost.
  Point at a remote host with `OLLAMA_HOST`.
- **Six more cloud engines** — `--mode deepseek | groq | mistral | openrouter |
  perplexity | xai`. All speak the OpenAI API (reusing the `[openai]` extra with
  their own key). **Perplexity's `sonar` models search the live web on every
  call** and their citations are folded into the report — a strong fit for a
  research tool. Groq is very fast; OpenRouter reaches hundreds of models with one
  key; DeepSeek is cheap with strong reasoning.
- **Recursive / multi-hop "drill" research** — `--drill N` makes the crew go
  *deep*, not just wide: after breadth-first coverage it picks the richest,
  best-supported finding and researches more specific follow-ups about it
  (mechanisms, causes, second-order effects, quantitative detail), then the next
  hop drills into the best remaining thread. Each deeper finding is fact-checked
  like any other. `--depth deep` now enables 2 drill hops automatically.
- **Per-engine endpoint override** — `VIBE_<PROVIDER>_BASE_URL` points any
  compatible engine at a proxy, gateway, or self-hosted deployment.
- `doctor` now shows a key-state grid for every engine and the drill setting.
- **TUI** — a **`Ctrl+X` stop** cancels the current run without quitting; the
  activity log shows a distinct "drill hop" line and the run header notes drill
  hops when enabled; the "Drilling deeper" stage has its own progress step.

### Fixed
- **TUI findings counter** no longer overflows (e.g. `findings 5/3`) when the
  self-refining or drill rounds add research threads beyond the initial plan.
- **TUI** cancelling a run can no longer leave the input box stuck disabled — the
  UI state resets before the backend is closed.

## [0.6.0]

### Added
- **More engines** — `--mode gemini | glm | kimi` run the pipeline on Google
  Gemini, Zhipu GLM, or Moonshot Kimi. All speak the OpenAI API, so they reuse the
  `[openai]` extra with their own key (`GEMINI_API_KEY` / `GLM_API_KEY` /
  `KIMI_API_KEY`) and auto-map Claude-named model defaults. (All pay-per-token; no
  subscription API path. Built-in web search for GLM; the others use model knowledge.)

### Fixed
- **PDF export no longer aborts** when the report references a remote image or a
  missing local one — such refs degrade to their caption instead of killing the
  whole PDF (matching the DOCX behaviour).
- **HTML export is XSS-safe** — raw HTML in LLM/web-derived report text and
  ```mermaid` blocks is escaped, so a scraped `<script>` can't execute when the
  report is opened locally.
- **Chart table fallback is loss-less** — every data point is kept even when the
  model supplies fewer labels than values.
- **TUI "Export all"** guards each format independently, so one failing export no
  longer skips the others.

## [0.5.0]

### Added
- **Visuals in reports** — the writer can emit **data charts** (```chart` JSON
  specs rendered to PNG via matplotlib, `[charts]` extra), **Mermaid diagrams**
  (rendered live in the HTML export), and **figure/image references** with source
  attribution. Charts and local images are embedded in the PDF and DOCX exports too.
- **Length & style control** — `--words N` / `--pages N` target a report length,
  and `--style report|essay|brief` sets the prose style (flowing paragraphs, not
  bullet dumps). `--no-charts` / `--no-diagrams` / `--no-figures` opt out.
- **OpenAI engine** — `--mode openai` runs the whole pipeline on OpenAI models via
  the Responses API (with web search), needing `OPENAI_API_KEY` and the `[openai]`
  extra. Claude-named model defaults are auto-mapped to `gpt-4o` / `gpt-4o-mini`;
  override with `--planner-model`/`--worker-model`. (OpenAI is pay-per-token — there
  is no subscription API path.)
- **TUI** now shows the **active engine + models** in the header and a **live
  call/token counter** in the status line as the run progresses.
- **TUI**: `Ctrl+E` exports the current report to every available format at once
  (HTML + PDF + DOCX + JSON), `Ctrl+O` opens the saved report, and completion now
  shows the **source-credibility tally** and a **cost estimate** — bringing the
  TUI to full parity with headless mode.

## [0.4.0]

### Added
- **Source credibility scoring** — every citation is classified into a tier
  (primary/authoritative → reputable news → organisation → blog/social) and the
  reference list is ranked by it.
- **Ranked "Sources" section** replacing the plain list; `--citations plain`
  restores the old flat list.
- **Disagreements & Conflicts section** — where fact-checkers flag conflicting or
  contested points, they're surfaced explicitly.
- **Source filtering** — `--only-domains gov,edu`, `--block-domains reddit.com`,
  and `--since YEAR` to steer and constrain sourcing.
- **Depth presets** — `--depth quick|standard|deep` bundles sub-questions, votes,
  and refinement rounds.
- **Per-stage model overrides** — `--verifier-model`, `--writer-model`,
  `--humanizer-model` (default to the planner model).
- **DOCX export** — `--docx` (needs the `[docx]` extra / `python-docx`).
- **`--debug`** — write a JSONL trace of every model call.
- Rough **cost estimate** and a **credibility tally** printed after headless runs.
- **CI** (GitHub Actions, Python 3.10–3.13) and a **PyPI publish** workflow.

### Fixed
- Credibility scoring now matches the URL **host only** — a junk link that embeds
  an authoritative domain in its path/query is no longer falsely elevated.
- The ranked Sources section is suppressed if the write-up already produced a
  references list under any common heading (References/Bibliography/Works cited).
- DOCX export now renders **tables** (previously dropped) and headings with inline
  formatting (previously emitted raw Markdown).

## [0.3.0]

### Added
- **Reliability**: exponential-backoff retry, per-call timeout, and a concurrency
  throttle around every model call; usage counters (calls/retries/tokens).
- **Graceful Ctrl+C** for headless runs.
- **Richer output**: a metadata header on every report, a structured **JSON
  sidecar** (`--json`), and findings kept in plan order.
- **HTML export** (`--html`), `--open`, and `--quiet` / `--verbose`.

## [0.2.0]

### Added
- **Multi-agent crew**: planner, parallel researchers, adversarial fact-check
  **debate** (voting), an editor that drives a **self-refining loop**, a
  synthesizer, and a **humanizer** final pass.
- **Data-validation layer** (`schemas.py`) — all agent output is parsed and
  validated; malformed model text is salvaged or safely rejected.
- **Persistent memory** — runs are distilled to disk and recalled for related
  topics; `memory` command to list/clear.
- **PDF export** (`--pdf`, Ctrl+P in the TUI).

## [0.1.0]

- Initial release: linear plan → research → verify → write pipeline, Textual TUI,
  API and subscription backends.

[0.7.0]: https://github.com/shalinda-j/Vibe-Research/releases
[0.6.0]: https://github.com/shalinda-j/Vibe-Research/releases
[0.5.0]: https://github.com/shalinda-j/Vibe-Research/releases
[0.4.0]: https://github.com/shalinda-j/Vibe-Research/releases
[0.3.0]: https://github.com/shalinda-j/Vibe-Research/releases
[0.2.0]: https://github.com/shalinda-j/Vibe-Research/releases
[0.1.0]: https://github.com/shalinda-j/Vibe-Research/releases
