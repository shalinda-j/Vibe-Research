## What & why

Briefly describe the change and the motivation.

## Checklist

- [ ] Tests added/updated and the full suite passes (`python -m unittest discover -s tests`)
- [ ] `README.md` / `CHANGELOG.md` updated if behaviour changed
- [ ] Core modules stay stdlib-only (heavy deps imported lazily)
- [ ] Model output that crosses an agent boundary is validated via `schemas.py`
