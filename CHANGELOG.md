# Changelog

All notable changes to **vibe-research** are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

### Added
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

[0.4.0]: https://github.com/shalinda-j/Vibe-Research/releases
[0.3.0]: https://github.com/shalinda-j/Vibe-Research/releases
[0.2.0]: https://github.com/shalinda-j/Vibe-Research/releases
[0.1.0]: https://github.com/shalinda-j/Vibe-Research/releases
