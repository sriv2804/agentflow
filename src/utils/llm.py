import asyncio
import os
import re
from typing import Optional
from abc import ABC, abstractmethod

from openai import AsyncOpenAI, InternalServerError, RateLimitError
import ollama as ollama_client


class LLMClient(ABC):
    """
    invoke() returns the response text. After each call, last_usage holds the
    token counts the inference API reported ({"tokens_in", "tokens_out"}, None
    when the provider did not report them) and last_retries the number of
    retried attempts, so callers (the trace) can read them without changing
    invoke()'s return type.
    """
    last_usage: Optional[dict] = None
    last_retries: int = 0

    @abstractmethod
    async def invoke(self, prompt: str) -> str:
        """Single prompt string in, response string out."""


def _usage(tokens_in, tokens_out) -> dict:
    as_int = lambda v: v if isinstance(v, int) else None
    return {"tokens_in": as_int(tokens_in), "tokens_out": as_int(tokens_out)}


class LLMError(Exception):
    """Raised when an LLM call fails."""


API_KEY_ENV = "AGENTFLOW_API_KEY"

#the SDK already retries 429s and 5xx, but only twice with sub-second backoff, and it only honours
#a Retry-After header; Gemini puts the wait in the error body instead ("Please retry in 26.6s") and
#returns frequent transient 503s under load, so both are retried here once the SDK gives up
LLM_RETRIES = 5
MAX_RETRY_WAIT = 120.0  # a longer hint means a daily quota is exhausted: fail instead of hanging
_RETRY_DELAY_RE = re.compile(r"(?:retryDelay['\"]?:\s*['\"]|retry in\s+)(\d+(?:\.\d+)?)s", re.IGNORECASE)


def retry_wait(err: Exception, attempt: int) -> float:
    """Server-suggested wait (429s carry one), else exponential backoff (2s, 4s, 8s, ...)."""
    match = _RETRY_DELAY_RE.search(str(getattr(err, "body", None) or err))
    if match:
        return float(match.group(1)) + 1.0  # small margin so the window has actually rolled over
    return min(2.0 * 2 ** attempt, 60.0)


def llm_settings(config: dict) -> dict:
    """config.yaml `llm` block with the AGENTFLOW_MODEL_* env vars applied on top."""
    llm_cfg = dict(config.get("llm") or {})
    env_backend = os.environ.get("AGENTFLOW_MODEL_BACKEND")
    if env_backend and env_backend != llm_cfg.get("backend"):
        #base_url / api_key_env in config.yaml belong to the configured backend,
        #so switching backend from the env falls back to the new backend's defaults
        llm_cfg = {"backend": env_backend, "model_name": llm_cfg.get("model_name")}
    if os.environ.get("AGENTFLOW_MODEL_NAME"):
        llm_cfg["model_name"] = os.environ["AGENTFLOW_MODEL_NAME"]
    if os.environ.get("AGENTFLOW_MODEL_BASE_URL"):
        llm_cfg["base_url"] = os.environ["AGENTFLOW_MODEL_BASE_URL"]
    return llm_cfg


def resolve_api_key(api_key_env: Optional[str] = None) -> Optional[str]:
    """AGENTFLOW_API_KEY first, then the env var named by llm.api_key_env, if any."""
    for name in (API_KEY_ENV, api_key_env):
        if name and os.environ.get(name):
            return os.environ[name]
    return None


def check_llm_settings(llm_cfg: dict) -> None:
    """Fail fast (at server startup) instead of on the first chat."""
    backend = llm_cfg.get("backend", "openai")
    if backend not in ("openai", "ollama"):
        raise ValueError(f"Unknown LLM backend: {backend!r}. Use 'openai' or 'ollama'.")
    if not llm_cfg.get("model_name"):
        raise ValueError("No LLM model configured: set llm.model_name in config.yaml or AGENTFLOW_MODEL_NAME.")
    if backend == "openai":
        if not llm_cfg.get("base_url"):
            raise ValueError(
                "The 'openai' LLM backend needs an endpoint: set llm.base_url in config.yaml or AGENTFLOW_MODEL_BASE_URL."
            )
        if not resolve_api_key(llm_cfg.get("api_key_env")):
            raise ValueError(f"The 'openai' LLM backend needs an API key: set {_key_hint(llm_cfg.get('api_key_env'))}.")


def _key_hint(api_key_env: Optional[str]) -> str:
    return f"{API_KEY_ENV} (or {api_key_env})" if api_key_env else API_KEY_ENV


class OpenAIClient(LLMClient):
    def __init__(
        self,
        model_name: str,
        base_url: Optional[str] = None,
        api_key_env: Optional[str] = None,
        temperature: Optional[float] = None,
    ):
        self.model_name = model_name
        self.temperature = temperature  # None: the provider's default (unchanged behaviour)
        if not base_url:
            raise LLMError("No endpoint for the openai backend: set llm.base_url in config.yaml or AGENTFLOW_MODEL_BASE_URL.")
        api_key = resolve_api_key(api_key_env)
        if not api_key:
            raise LLMError(f"No API key for the openai backend: set {_key_hint(api_key_env)}.")
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
        )

    async def invoke(self, prompt: str) -> str:
        self.last_usage, self.last_retries = None, 0
        opts = {} if self.temperature is None else {"temperature": self.temperature}
        for attempt in range(LLM_RETRIES + 1):
            self.last_retries = attempt
            try:
                response = await self._client.chat.completions.create(
                    model=self.model_name,
                    messages=[{"role": "user", "content": prompt}],
                    **opts,
                )
                usage = getattr(response, "usage", None)
                self.last_usage = _usage(getattr(usage, "prompt_tokens", None),
                                         getattr(usage, "completion_tokens", None))
                return response.choices[0].message.content
            except (RateLimitError, InternalServerError) as e:
                wait = retry_wait(e, attempt)
                if attempt == LLM_RETRIES or wait > MAX_RETRY_WAIT:
                    raise LLMError(f"OpenAI call failed after {attempt + 1} attempts: {e}") from e
                print(f"LLM call failed ({e.status_code}), retrying in {wait:.0f}s ({attempt + 1}/{LLM_RETRIES})")
                await asyncio.sleep(wait)
            except Exception as e:
                raise LLMError(f"OpenAI call failed: {e}") from e


class OllamaClient(LLMClient):
    def __init__(self, model_name: str, base_url: str = "http://localhost:11434",
                 temperature: Optional[float] = None):
        self.model_name = model_name
        self.temperature = temperature  # None: the model's default (unchanged behaviour)
        self._client = ollama_client.AsyncClient(host=base_url)

    async def invoke(self, prompt: str) -> str:
        self.last_usage, self.last_retries = None, 0
        opts = {} if self.temperature is None else {"options": {"temperature": self.temperature}}
        try:
            response = await self._client.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
                **opts,
            )
            self.last_usage = _usage(getattr(response, "prompt_eval_count", None),
                                     getattr(response, "eval_count", None))
            return response.message.content
        except Exception as e:
            raise LLMError(f"Ollama call failed: {e}") from e


class LLM:
    def __init__(
        self,
        model_name: str,
        backend: str = "openai",
        base_url: Optional[str] = None,
        api_key_env: Optional[str] = None,
        temperature: Optional[float] = None,
    ):
        #only forward what's set so each client keeps its own defaults
        opts = {"base_url": base_url} if base_url else {}
        if temperature is not None:
            opts["temperature"] = temperature
        if backend == "openai":
            if api_key_env:
                opts["api_key_env"] = api_key_env
            self.client: LLMClient = OpenAIClient(model_name, **opts)
        elif backend == "ollama":
            self.client = OllamaClient(model_name, **opts)
        else:
            raise ValueError(f"Unknown backend: {backend!r}. Use 'openai' or 'ollama'.")

    async def invoke(self, prompt: str) -> str:
        return await self.client.invoke(prompt)

    @property
    def last_usage(self) -> Optional[dict]:
        return getattr(self.client, "last_usage", None)

    @property
    def last_retries(self) -> int:
        return getattr(self.client, "last_retries", 0)
