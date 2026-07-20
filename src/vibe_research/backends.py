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


# OpenAI-compatible providers. Gemini, GLM (Zhipu), Kimi (Moonshot), DeepSeek,
# Groq, Mistral, OpenRouter, Perplexity, xAI (Grok) and Ollama all expose an
# OpenAI-style chat-completions endpoint, so they run through the same `openai`
# client with a different base_url — no extra dependency. Each entry gives the
# endpoint, which env var(s) hold the key, sensible strong/fast default models
# (so Claude-named config defaults auto-map on `--mode <provider>`), and whether a
# simple inline web-search tool is available. Most are pay-per-token API keys —
# none offer a subscription-login API path — and one (Ollama) runs fully local
# and free (``local: True`` → no key required). The ``base_url`` for any provider
# can be overridden at runtime with ``VIBE_<PROVIDER>_BASE_URL`` (e.g. to point at
# a proxy, a self-hosted gateway, or a remote Ollama host).
_PROVIDERS = {
    "openai": {
        "base_url": None,  # native OpenAI (Responses API), handled by OpenAIBackend
        "key_env": ("OPENAI_API_KEY",),
        "strong": "gpt-4o", "fast": "gpt-4o-mini", "search": "openai",
    },
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        "strong": "gemini-2.5-pro", "fast": "gemini-2.5-flash", "search": None,
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        "key_env": ("GLM_API_KEY", "ZHIPUAI_API_KEY"),
        "strong": "glm-4.6", "fast": "glm-4-flash", "search": "glm",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "key_env": ("KIMI_API_KEY", "MOONSHOT_API_KEY"),
        "strong": "kimi-k2-0905-preview", "fast": "moonshot-v1-8k", "search": None,
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "key_env": ("DEEPSEEK_API_KEY",),
        "strong": "deepseek-reasoner", "fast": "deepseek-chat", "search": None,
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "key_env": ("GROQ_API_KEY",),
        "strong": "llama-3.3-70b-versatile", "fast": "llama-3.1-8b-instant", "search": None,
    },
    "mistral": {
        "base_url": "https://api.mistral.ai/v1",
        "key_env": ("MISTRAL_API_KEY",),
        "strong": "mistral-large-latest", "fast": "mistral-small-latest", "search": None,
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "key_env": ("OPENROUTER_API_KEY",),
        "strong": "openai/gpt-4o", "fast": "openai/gpt-4o-mini", "search": None,
    },
    "perplexity": {
        # Perplexity's `sonar` models search the live web on every call — no tool
        # needed — and return the URLs they used in a top-level `citations` field.
        "base_url": "https://api.perplexity.ai",
        "key_env": ("PERPLEXITY_API_KEY", "PPLX_API_KEY"),
        "strong": "sonar-pro", "fast": "sonar", "search": "builtin",
    },
    "xai": {
        "base_url": "https://api.x.ai/v1",
        "key_env": ("XAI_API_KEY", "GROK_API_KEY"),
        "strong": "grok-2-latest", "fast": "grok-2-latest", "search": None,
    },
    "ollama": {
        # Local, free, offline. No API key; the OpenAI client just needs a
        # placeholder. Point elsewhere with OLLAMA_HOST or VIBE_OLLAMA_BASE_URL.
        "base_url": "http://localhost:11434/v1",
        "key_env": (),
        "strong": "llama3.1", "fast": "llama3.1", "search": None, "local": True,
    },
}

# Providers that run on the shared OpenAI-compatible chat-completions backend.
COMPATIBLE_PROVIDERS = (
    "gemini", "glm", "kimi", "deepseek", "groq", "mistral",
    "openrouter", "perplexity", "xai", "ollama",
)


def _provider_key(provider: str) -> str | None:
    for env in _PROVIDERS.get(provider, {}).get("key_env", ()):
        val = os.environ.get(env)
        if val:
            return val
    return None


def _provider_base_url(provider: str) -> str | None:
    """The effective base URL for a compatible provider.

    A ``VIBE_<PROVIDER>_BASE_URL`` env var wins for any provider (proxy / gateway
    / self-hosted). Ollama additionally honours the conventional ``OLLAMA_HOST``,
    appending the ``/v1`` OpenAI path if the host was given without it.
    """
    override = os.environ.get(f"VIBE_{provider.upper()}_BASE_URL")
    if override:
        return override
    if provider == "ollama":
        host = os.environ.get("OLLAMA_HOST")
        if host:
            host = host.rstrip("/")
            return host if host.endswith("/v1") else host + "/v1"
    return _PROVIDERS.get(provider, {}).get("base_url")


def _pick_model(model: str, provider: str, tier: str) -> str:
    """A provider-appropriate model: keep an explicit non-Claude name, else the
    provider's default for this tier (strong=planner, fast=worker)."""
    prov = _PROVIDERS.get(provider)
    if not prov:
        return model
    low = (model or "").lower()
    if not model or low.startswith("claude"):
        return prov[tier]
    return model


def map_openai_model(model: str) -> str:
    """Translate a Claude-style model name to an OpenAI one; pass others through."""
    low = (model or "").lower()
    if not model or not low.startswith("claude"):
        return model or _PROVIDERS["openai"]["strong"]
    tier = "fast" if ("sonnet" in low or "haiku" in low) else "strong"
    return _PROVIDERS["openai"][tier]


def resolve_models(provider: str, planner: str, worker: str) -> tuple[str, str]:
    """Resolve the effective (planner, worker) models for the chosen provider."""
    if provider not in _PROVIDERS:
        return planner, worker
    return _pick_model(planner, provider, "strong"), _pick_model(worker, provider, "fast")


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


class OpenAIBackend(Backend):
    """OpenAI models via the Responses API. Pay-per-token; needs OPENAI_API_KEY.

    Note: OpenAI has no subscription-billed API (unlike Anthropic's Agent SDK
    path) — API usage is always metered against your OpenAI account. Web search
    uses the Responses API's built-in ``web_search_preview`` tool; override the
    tool type with ``VIBE_OPENAI_SEARCH_TOOL`` if your account/model uses a
    different name.
    """

    name = "openai"

    def __init__(self) -> None:
        super().__init__()
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError(
                "OpenAI mode needs the 'openai' package.\n"
                "    pip install openai\n"
                '    (or: pip install "vibe-research[openai]")'
            ) from exc
        if not os.environ.get("OPENAI_API_KEY"):
            raise RuntimeError(
                "OpenAI mode needs OPENAI_API_KEY.\n"
                "    Get a key at https://platform.openai.com/api-keys\n"
                "    export OPENAI_API_KEY=sk-...\n"
                "    (OpenAI bills per token — there is no subscription API path.)"
            )
        self._client = AsyncOpenAI()
        self._search_tool = os.environ.get("VIBE_OPENAI_SEARCH_TOOL", "web_search_preview")

    async def _complete_once(
        self, prompt: str, model: str, use_search: bool = False
    ) -> tuple[str, list[str]]:
        model = map_openai_model(model)
        kwargs = {"model": model, "input": prompt}
        if use_search:
            kwargs["tools"] = [{"type": self._search_tool}]
        response = await self._client.responses.create(**kwargs)

        usage = getattr(response, "usage", None)
        if usage is not None:
            self.input_tokens += int(getattr(usage, "input_tokens", 0) or 0)
            self.output_tokens += int(getattr(usage, "output_tokens", 0) or 0)

        text = getattr(response, "output_text", "") or ""
        urls: list[str] = []
        for item in getattr(response, "output", None) or []:
            for content in getattr(item, "content", None) or []:
                for ann in getattr(content, "annotations", None) or []:
                    url = getattr(ann, "url", None)
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


class CompatibleBackend(Backend):
    """Any OpenAI-compatible chat-completions endpoint.

    Covers Gemini, GLM, Kimi, DeepSeek, Groq, Mistral, OpenRouter, Perplexity,
    xAI (Grok) and local Ollama — all through the same ``openai`` client with the
    provider's ``base_url`` and API key. Most are pay-per-token (no subscription
    path); Ollama runs local and free (no key). Live web search is wired where the
    provider exposes it — GLM's inline tool, and Perplexity's `sonar` models which
    search on every call — otherwise the model answers from its own knowledge (the
    fact-checker then scores sourcing accordingly).
    """

    def __init__(self, provider: str) -> None:
        super().__init__()
        prov = _PROVIDERS.get(provider)
        if prov is None or provider not in COMPATIBLE_PROVIDERS:
            raise RuntimeError(f"unknown provider: {provider}")
        self.name = provider
        self._prov = prov
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:
            raise RuntimeError(
                f"{provider} mode needs the 'openai' package (it speaks the OpenAI API).\n"
                "    pip install openai\n"
                '    (or: pip install "vibe-research[openai]")'
            ) from exc
        key = _provider_key(provider)
        if not key and not prov.get("local"):
            envs = " or ".join(prov["key_env"])
            raise RuntimeError(
                f"{provider} mode needs an API key.\n"
                f"    export {prov['key_env'][0]}=...   (checked: {envs})\n"
                f"    ({provider} bills per token — there is no subscription API path.)"
            )
        # Local engines (Ollama) need no real key, but the OpenAI client still
        # requires a non-empty placeholder string.
        self._client = AsyncOpenAI(
            api_key=key or "local", base_url=_provider_base_url(provider)
        )

    async def _complete_once(
        self, prompt: str, model: str, use_search: bool = False
    ) -> tuple[str, list[str]]:
        model = _pick_model(model, self.name, "strong")
        kwargs = {"model": model, "messages": [{"role": "user", "content": prompt}]}
        if use_search and self._prov.get("search") == "glm":
            # Zhipu GLM's inline web-search tool. (Perplexity's `sonar` models
            # search automatically, so they need no tool here.)
            kwargs["tools"] = [{"type": "web_search", "web_search": {"enable": True}}]
        response = await self._client.chat.completions.create(**kwargs)

        usage = getattr(response, "usage", None)
        if usage is not None:
            self.input_tokens += int(getattr(usage, "prompt_tokens", 0) or 0)
            self.output_tokens += int(getattr(usage, "completion_tokens", 0) or 0)

        text = ""
        if getattr(response, "choices", None):
            text = getattr(response.choices[0].message, "content", "") or ""
        urls = extract_urls(text)
        # Some search-native providers (Perplexity `sonar`) return the sources
        # they used in a top-level `citations` list rather than inline in the
        # text — fold those in so the report stays fully cited.
        for cite in getattr(response, "citations", None) or []:
            url = cite if isinstance(cite, str) else getattr(cite, "url", None)
            if url and url not in urls:
                urls.append(url)
        return text, urls

    async def aclose(self) -> None:
        try:
            await self._client.close()
        except Exception:
            pass


def detect_available() -> dict:
    """Report which backends' prerequisites are present. Never imports them."""
    # Keys for every compatible provider, keyed by name. Ollama is local/keyless,
    # so "availability" is whether its server is reachable at runtime, not a key —
    # reported separately below.
    provider_keys = {
        p: (_provider_key(p) is not None)
        for p in COMPATIBLE_PROVIDERS
        if not _PROVIDERS[p].get("local")
    }
    return {
        "anthropic": importlib.util.find_spec("anthropic") is not None,
        "claude_agent_sdk": importlib.util.find_spec("claude_agent_sdk") is not None,
        "openai": importlib.util.find_spec("openai") is not None,
        "textual": importlib.util.find_spec("textual") is not None,
        "api_key_set": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "openai_key_set": bool(os.environ.get("OPENAI_API_KEY")),
        # Back-compat individual flags (kept for existing callers/tests).
        "gemini_key_set": _provider_key("gemini") is not None,
        "glm_key_set": _provider_key("glm") is not None,
        "kimi_key_set": _provider_key("kimi") is not None,
        # New: every compatible provider's key state, plus the local engine.
        "provider_keys": provider_keys,
        "ollama_local": True,
    }


def choose_mode(mode: str) -> str:
    """Resolve 'auto' to a concrete backend name, or validate an explicit choice."""
    if mode in ("api", "subscription", "openai") or mode in COMPATIBLE_PROVIDERS:
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
    if chosen == "openai":
        return OpenAIBackend()
    if chosen in COMPATIBLE_PROVIDERS:
        return CompatibleBackend(chosen)
    if chosen == "api":
        return APIBackend()
    return SubscriptionBackend()
