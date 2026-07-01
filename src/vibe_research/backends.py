"""Model backends for vibe-research.

Two interchangeable engines behind one interface:

  * APIBackend          -> raw Anthropic Messages API (pay-per-token, needs an API key)
  * SubscriptionBackend -> Claude Agent SDK (draws from your Claude subscription)

Reliability lives in the base class so every engine gets it for free. The public
``complete()`` wraps each real call (``_complete_once``) with:

  * a concurrency throttle  — cap simultaneous calls so we don't hammer the API,
  * exponential-backoff retry — transient 429 / 5xx / timeout / connection errors
    are retried instead of sinking the run,
  * a per-call timeout       — a hung call can't stall the pipeline forever,
  * usage counters           — calls, retries, failures, and (API mode) tokens.

Heavy third-party packages (anthropic, claude_agent_sdk) are imported lazily
*inside* the classes, so importing this module never requires them. That keeps
`vibe-research doctor`, `--help`, and the offline test-suite working with only
the standard library present.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import random
import re
from abc import ABC, abstractmethod

_URL_RE = re.compile(r"https?://[^\s\)\]\}>\"']+")

# Defaults; overridable per-instance via configure().
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_BASE_DELAY = 1.5      # seconds; grows as base_delay * 2**attempt
_DEFAULT_CALL_TIMEOUT = 180.0  # seconds; 0 disables
_DEFAULT_MAX_CONCURRENCY = 4


def extract_urls(text: str) -> list[str]:
    """Pull unique http(s) URLs out of free text, trimming trailing punctuation."""
    seen: set[str] = set()
    out: list[str] = []
    for match in _URL_RE.findall(text or ""):
        url = match.rstrip(".,;:")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


def estimate_cost(usage: dict) -> str:
    """A rough $ estimate from token counts (API mode only). Clearly approximate:
    the real cost depends on which model ran each stage. Empty for subscription
    mode (no per-token data)."""
    it = int((usage or {}).get("input_tokens", 0) or 0)
    ot = int((usage or {}).get("output_tokens", 0) or 0)
    if not (it or ot):
        return ""
    cost = it / 1_000_000 * 5.0 + ot / 1_000_000 * 15.0  # blended Opus-class rate
    return f"est. cost ~${cost:.2f}  ({it:,} in + {ot:,} out tokens, rough)"


def _is_retryable(exc: BaseException) -> bool:
    """Whether an exception from a model call is worth retrying.

    Recognised by class-name/attribute rather than importing the SDK, so it works
    for both anthropic and the Agent SDK. Auth (401/403) and bad-request (400)
    errors are deliberately *not* retryable — retrying them just wastes time.
    """
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, TimeoutError)):
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    name = type(exc).__name__.lower()
    tells = ("ratelimit", "timeout", "connection", "internalserver",
             "overloaded", "serviceunavailable", "apiconnection", "502", "503")
    return any(t in name for t in tells)


class Backend(ABC):
    name: str = "backend"

    def __init__(self) -> None:
        self.max_retries = _DEFAULT_MAX_RETRIES
        self.base_delay = _DEFAULT_BASE_DELAY
        self.call_timeout = _DEFAULT_CALL_TIMEOUT
        self._sem = asyncio.Semaphore(_DEFAULT_MAX_CONCURRENCY)
        self._debug_path = None
        # usage counters
        self.calls = 0
        self.retries = 0
        self.failures = 0
        self.input_tokens = 0
        self.output_tokens = 0

    def configure(
        self,
        *,
        max_retries: int | None = None,
        call_timeout: float | None = None,
        max_concurrency: int | None = None,
        debug_path=None,
    ) -> None:
        """Apply reliability settings (typically from user config)."""
        if max_retries is not None:
            self.max_retries = max(0, int(max_retries))
        if call_timeout is not None:
            self.call_timeout = max(0.0, float(call_timeout))
        if max_concurrency is not None:
            self._sem = asyncio.Semaphore(max(1, int(max_concurrency)))
        if debug_path is not None:
            self._debug_path = str(debug_path)

    def _trace(self, model: str, use_search: bool, prompt: str, text: str) -> None:
        """Append one prompt/response record to the debug trace, if enabled.

        Best-effort and bounded: never let logging break a run, and truncate so a
        trace file can't balloon. Prompts/responses are stored head-only."""
        if not self._debug_path:
            return
        import json

        record = {
            "call": self.calls,
            "model": model,
            "search": use_search,
            "prompt_chars": len(prompt or ""),
            "response_chars": len(text or ""),
            "prompt_head": (prompt or "")[:500],
            "response_head": (text or "")[:500],
        }
        try:
            with open(self._debug_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError:
            pass

    @abstractmethod
    async def _complete_once(
        self, prompt: str, model: str, use_search: bool = False
    ) -> tuple[str, list[str]]:
        """Run one real model turn. Return (answer_text, source_urls)."""
        raise NotImplementedError

    async def complete(
        self, prompt: str, model: str, use_search: bool = False
    ) -> tuple[str, list[str]]:
        """Throttled, retried, timed-out wrapper around :meth:`_complete_once`."""
        self.calls += 1
        last_exc: BaseException | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self._sem:
                    if self.call_timeout and self.call_timeout > 0:
                        result = await asyncio.wait_for(
                            self._complete_once(prompt, model, use_search),
                            timeout=self.call_timeout,
                        )
                    else:
                        result = await self._complete_once(prompt, model, use_search)
                self._trace(model, use_search, prompt, result[0] if result else "")
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - deliberately broad; decide via _is_retryable
                last_exc = exc
                if attempt >= self.max_retries or not _is_retryable(exc):
                    self.failures += 1
                    raise
                self.retries += 1
                # Exponential backoff with jitter (proportional to base_delay, so a
                # base_delay of 0 — e.g. in tests — means no sleep) to de-sync threads.
                delay = self.base_delay * (2 ** attempt) + random.uniform(0, self.base_delay)
                await asyncio.sleep(delay)
        # Unreachable, but keep the type checker and logic honest.
        assert last_exc is not None
        raise last_exc

    def usage(self) -> dict:
        """Snapshot of how much this backend has been used this run."""
        return {
            "calls": self.calls,
            "retries": self.retries,
            "failures": self.failures,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
        }

    def usage_line(self) -> str:
        u = self.usage()
        parts = [f"{u['calls']} calls"]
        if u["retries"]:
            parts.append(f"{u['retries']} retries")
        if u["failures"]:
            parts.append(f"{u['failures']} failed")
        if u["input_tokens"] or u["output_tokens"]:
            parts.append(f"{u['input_tokens']}+{u['output_tokens']} tokens")
        return " · ".join(parts)

    async def aclose(self) -> None:
        return None


class APIBackend(Backend):
    """Raw Messages API. Every token is billed to your Console account."""

    name = "api"

    def __init__(self) -> None:
        super().__init__()
        try:
            from anthropic import AsyncAnthropic
        except ImportError as exc:
            raise RuntimeError(
                "API mode needs the 'anthropic' package.\n"
                "    pip install anthropic"
            ) from exc
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "API mode needs ANTHROPIC_API_KEY.\n"
                "    Get a key at https://console.anthropic.com\n"
                "    export ANTHROPIC_API_KEY=sk-ant-..."
            )
        self._client = AsyncAnthropic()

    async def _complete_once(
        self, prompt: str, model: str, use_search: bool = False
    ) -> tuple[str, list[str]]:
        kwargs = {
            "model": model,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        if use_search:
            kwargs["tools"] = [
                {"type": "web_search_20250305", "name": "web_search", "max_uses": 5}
            ]
        message = await self._client.messages.create(**kwargs)

        usage = getattr(message, "usage", None)
        if usage is not None:
            self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)

        text = "".join(
            block.text for block in message.content
            if getattr(block, "type", None) == "text"
        )
        urls: list[str] = []
        for block in message.content:
            for citation in getattr(block, "citations", None) or []:
                url = getattr(citation, "url", None)
                if url and url not in urls:
                    urls.append(url)
        for url in extract_urls(text):
            if url not in urls:
                urls.append(url)
        return text, urls

    async def aclose(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass


class SubscriptionBackend(Backend):
    """Claude Agent SDK. Usage draws from your Claude subscription quota.

    Requires the Claude Code engine installed and a subscription login, and
    ANTHROPIC_API_KEY must be UNSET (otherwise it shadows the subscription and
    you get billed per token).
    """

    name = "subscription"

    def __init__(self) -> None:
        super().__init__()
        try:
            from claude_agent_sdk import query, ClaudeAgentOptions
        except ImportError as exc:
            raise RuntimeError(
                "Subscription mode needs the Agent SDK + Claude Code engine:\n"
                "    1) npm install -g @anthropic-ai/claude-code\n"
                "    2) claude          (then type /login and sign in)\n"
                "    3) pip install claude-agent-sdk\n"
                "    4) unset ANTHROPIC_API_KEY   (or it bills you per token)"
            ) from exc
        if os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "ANTHROPIC_API_KEY is set, which would bill you per token instead of\n"
                "using your subscription. Run:  unset ANTHROPIC_API_KEY"
            )
        self._query = query
        self._Options = ClaudeAgentOptions

    async def _complete_once(
        self, prompt: str, model: str, use_search: bool = False
    ) -> tuple[str, list[str]]:
        tools = ["WebSearch", "WebFetch"] if use_search else []
        result = ""
        async for message in self._query(
            prompt=prompt,
            options=self._Options(model=model, allowed_tools=tools),
        ):
            candidate = getattr(message, "result", None)
            if isinstance(candidate, str):
                result = candidate
        return result, extract_urls(result)


def detect_available() -> dict:
    """Report which backends' prerequisites are present. Never imports them."""
    return {
        "anthropic": importlib.util.find_spec("anthropic") is not None,
        "claude_agent_sdk": importlib.util.find_spec("claude_agent_sdk") is not None,
        "textual": importlib.util.find_spec("textual") is not None,
        "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }


def choose_mode(mode: str) -> str:
    """Resolve 'auto' to a concrete backend name, or validate an explicit choice."""
    if mode in ("api", "subscription"):
        return mode
    avail = detect_available()
    if avail["api_key_set"]:
        if avail["anthropic"]:
            return "api"
        raise RuntimeError(
            "ANTHROPIC_API_KEY is set but the 'anthropic' package isn't installed.\n"
            "    pip install anthropic"
        )
    if avail["claude_agent_sdk"]:
        return "subscription"
    if avail["anthropic"]:
        return "api"  # will raise a clear 'needs API key' message on use
    raise RuntimeError(
        "No backend available. Pick one:\n"
        "    API mode:          pip install anthropic ; export ANTHROPIC_API_KEY=...\n"
        "    Subscription mode: pip install \"vibe-research[subscription]\" ; set up Claude Code login"
    )


def get_backend(mode: str = "auto") -> Backend:
    chosen = choose_mode(mode)
    return APIBackend() if chosen == "api" else SubscriptionBackend()
