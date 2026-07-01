"""Configuration for vibe-research.

Config lives at ~/.config/vibe-research/config.json (or $XDG_CONFIG_HOME).
Reports default to ~/.local/share/vibe-research/reports (or $XDG_DATA_HOME).
Everything here is stdlib-only so it works before any heavy deps are installed.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

APP_NAME = "vibe-research"


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / APP_NAME


def config_path() -> Path:
    return config_dir() / "config.json"


def _data_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / APP_NAME


def default_reports_dir() -> Path:
    return _data_root() / "reports"


def default_memory_dir() -> Path:
    return _data_root() / "memory"


@dataclass
class Config:
    mode: str = "auto"                       # auto|api|subscription|openai|gemini|glm|kimi
    planner_model: str = "claude-opus-4-8"   # planning, fact-check, edit, write
    worker_model: str = "claude-sonnet-4-6"  # the many web-search calls
    max_parallel: int = 2                    # concurrent research threads
    subquestions: int = 5                    # how many sub-questions to research
    reports_dir: str = ""                    # empty -> default_reports_dir()

    # --- autonomous multi-agent knobs ---------------------------------------
    max_iterations: int = 2                  # self-refining rounds (gap-filling)
    verifier_votes: int = 2                  # adversarial fact-checkers per finding
    quality_threshold: float = 0.75          # editor confidence needed to stop
    enable_debate: bool = True               # multi-verifier voting vs single check
    enable_memory: bool = True               # learn from / persist past runs
    humanize: bool = True                    # final pass: rewrite in a natural human voice
    memory_dir: str = ""                     # empty -> default_memory_dir()

    # --- per-stage model overrides (empty -> use planner_model) --------------
    verifier_model: str = ""                 # fact-check model
    writer_model: str = ""                   # synthesis model
    humanizer_model: str = ""                # human-voice rewrite model

    # --- sourcing controls ---------------------------------------------------
    citations: str = "ranked"                # ranked (credibility) | plain
    since_year: int = 0                      # prefer sources >= this year (0 = off)
    only_domains: str = ""                   # comma-sep domain substrings to keep
    block_domains: str = ""                  # comma-sep domain substrings to drop

    # --- writing style / length / visuals ------------------------------------
    prose_style: str = "report"              # report | essay | brief
    words: int = 0                           # target word count (0 = model decides)
    enable_charts: bool = True               # render ```chart data blocks to images
    enable_diagrams: bool = True             # allow ```mermaid diagrams
    enable_figures: bool = True              # allow embedded figure/image references

    # --- output --------------------------------------------------------------
    export_pdf: bool = False                  # also write a PDF beside each report
    export_html: bool = False                 # also write an HTML page beside each report
    export_json: bool = False                 # also write a structured JSON sidecar
    export_docx: bool = False                 # also write a Word .docx beside each report
    open_after: bool = False                  # open the report when a run finishes
    debug: bool = False                       # write a JSONL trace of every model call

    # --- reliability knobs ---------------------------------------------------
    max_retries: int = 3                     # backoff retries per model call (0 = none)
    call_timeout: int = 180                  # per-call timeout in seconds (0 = none)
    max_concurrency: int = 4                 # cap on simultaneous model calls

    def resolved_reports_dir(self) -> Path:
        return Path(self.reports_dir).expanduser() if self.reports_dir else default_reports_dir()

    def resolved_memory_dir(self) -> Path:
        return Path(self.memory_dir).expanduser() if self.memory_dir else default_memory_dir()


def default_config() -> Config:
    return Config()


def load_config() -> Config:
    path = config_path()
    if not path.exists():
        return default_config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default_config()
    cfg = default_config()
    for key, value in (data or {}).items():
        if not hasattr(cfg, key):
            continue
        try:
            # Route through the same validation the CLI uses, so a hand-edited or
            # corrupted config.json (e.g. {"quality_threshold": "high"}) can't slip
            # a wrong-typed value through to crash `doctor` or the pipeline later.
            apply_setting(cfg, key, str(value))
        except (ValueError, KeyError):
            pass  # keep the default for this field
    return cfg


def save_config(cfg: Config) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    return path


_INT_FIELDS = {"max_parallel", "subquestions", "max_iterations", "verifier_votes", "max_concurrency"}
_NONNEG_INT_FIELDS = {"max_retries", "call_timeout", "since_year", "words"}   # 0 is meaningful (off)
_FLOAT_FIELDS = {"quality_threshold"}
_BOOL_FIELDS = {
    "enable_debate", "enable_memory", "humanize",
    "export_pdf", "export_html", "export_json", "export_docx", "open_after", "debug",
    "enable_charts", "enable_diagrams", "enable_figures",
}
_ALLOWED_MODES = {"auto", "api", "subscription", "openai", "gemini", "glm", "kimi"}
_ALLOWED_CITATIONS = {"ranked", "plain"}
_ALLOWED_STYLES = {"report", "essay", "brief"}
_TRUE = {"1", "true", "yes", "on", "y"}
_FALSE = {"0", "false", "no", "off", "n"}


def _parse_bool(value: str) -> bool:
    v = value.strip().lower()
    if v in _TRUE:
        return True
    if v in _FALSE:
        return False
    raise ValueError(value)


def apply_setting(cfg: Config, key: str, value: str) -> None:
    """Apply a single KEY=VALUE edit to a Config, with light validation."""
    if not hasattr(cfg, key):
        raise KeyError(key)
    if key in _INT_FIELDS:
        n = int(value)                          # raises ValueError on bad input
        if n < 1:
            raise ValueError(value)             # these knobs are all >= 1
        setattr(cfg, key, n)
    elif key in _NONNEG_INT_FIELDS:
        n = int(value)                          # raises ValueError on bad input
        if n < 0:
            raise ValueError(value)             # 0 allowed (means "off")
        setattr(cfg, key, n)
    elif key in _FLOAT_FIELDS:
        f = float(value)                        # raises ValueError on bad input
        if not 0.0 <= f <= 1.0:
            raise ValueError(value)
        setattr(cfg, key, f)
    elif key in _BOOL_FIELDS:
        setattr(cfg, key, _parse_bool(value))
    elif key == "mode":
        if value not in _ALLOWED_MODES:
            raise ValueError(value)
        setattr(cfg, key, value)
    elif key == "citations":
        if value not in _ALLOWED_CITATIONS:
            raise ValueError(value)
        setattr(cfg, key, value)
    elif key == "prose_style":
        if value not in _ALLOWED_STYLES:
            raise ValueError(value)
        setattr(cfg, key, value)
    else:
        setattr(cfg, key, value)
