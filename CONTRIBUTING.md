# Contributing to vibe-research

Thanks for your interest! This is a small, focused project — contributions that
keep it lean and well-tested are very welcome.

## Development setup

```bash
git clone https://github.com/shalinda-j/Vibe-Research.git
cd Vibe-Research
pip install -e ".[dev,pdf,docx]"
```

## Running the tests

The suite is **offline** — it drives the whole multi-agent pipeline against a
fake backend, so it needs no API key or network:

```bash
python -m unittest discover -s tests -v
# or
make test
```

Please make sure the full suite passes before opening a PR, and add tests for any
new behaviour.

## Design principles

- **stdlib-first for core logic.** `config.py`, `schemas.py`, `enrich.py`,
  `memory.py`, and `reports.py` avoid heavy imports so `doctor`, `--help`,
  `config`, and the offline tests work before anything is installed. Heavy deps
  (`anthropic`, `textual`, `fpdf2`, `python-docx`) are imported lazily inside the
  functions that need them.
- **Untrusted model output is validated at the boundary** (`schemas.py`) — parse
  into a typed object or reject with a clear error; never trust raw text.
- **Every agent stage degrades gracefully.** A transient backend failure should
  downgrade one finding, not crash the run.
- **Honesty by design.** Keep the fact-check, the "Confidence & Gaps" section, and
  the credibility ranking intact — the tool's value is *cited and honest*, not
  "always right".

## Pull requests

1. Branch from `main`.
2. Keep changes focused; match the surrounding style and comment density.
3. Add/adjust tests; run the suite.
4. Update `README.md` and `CHANGELOG.md` when behaviour changes.

By contributing you agree your work is licensed under the project's MIT license.
