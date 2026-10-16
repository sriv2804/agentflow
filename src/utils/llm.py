import os
from typing import Optional
from abc import ABC, abstractmethod

from openai import AsyncOpenAI
import ollama as ollama_client


class LLMClient(ABC):
    @abstractmethod
    async def invoke(self, prompt: str) -> str:
        """Single prompt string in, response string out."""


class LLMError(Exception):
    """Raised when an LLM call fails."""


API_KEY_ENV = "AGENTFLOW_API_KEY"
DEFAULT_API_KEY_ENV = "GITHUB_TOKEN"


def llm_settings(config: dict) -> dict:
    """config.yaml `llm` block with AGENTFLOW_MODEL_BACKEND / AGENTFLOW_MODEL_NAME applied on top."""
    llm_cfg = dict(config.get("llm") or {})
    env_backend = os.environ.get("AGENTFLOW_MODEL_BACKEND")
    if env_backend and env_backend != llm_cfg.get("backend"):
        #base_url / api_key_env in config.yaml belong to the configured backend,
        #so switching backend from the env falls back to the new backend's defaults
        llm_cfg = {"backend": env_backend, "model_name": llm_cfg.get("model_name")}
    if os.environ.get("AGENTFLOW_MODEL_NAME"):
        llm_cfg["model_name"] = os.environ["AGENTFLOW_MODEL_NAME"]
    return llm_cfg


def resolve_api_key(api_key_env: Optional[str] = None) -> Optional[str]:
    """AGENTFLOW_API_KEY first, then the configured env var (GITHUB_TOKEN by default)."""
    for name in (API_KEY_ENV, api_key_env or DEFAULT_API_KEY_ENV):
        if os.environ.get(name):
            return os.environ[name]
    return None


def check_llm_settings(llm_cfg: dict) -> None:
    """Fail fast (at server startup) instead of on the first chat."""
    backend = llm_cfg.get("backend", "openai")
    if backend not in ("openai", "ollama"):
        raise ValueError(f"Unknown LLM backend: {backend!r}. Use 'openai' or 'ollama'.")
    if not llm_cfg.get("model_name"):
        raise ValueError("No LLM model configured: set llm.model_name in config.yaml or AGENTFLOW_MODEL_NAME.")
    if backend == "openai" and not resolve_api_key(llm_cfg.get("api_key_env")):
        fallback = llm_cfg.get("api_key_env") or DEFAULT_API_KEY_ENV
        raise ValueError(
            f"The 'openai' LLM backend needs an API key: set {API_KEY_ENV} (or {fallback})."
        )


class OpenAIClient(LLMClient):
    def __init__(
        self,
        model_name: str,
        base_url: str = "https://models.inference.ai.azure.com",
        api_key_env: Optional[str] = None,
    ):
        self.model_name = model_name
        api_key = resolve_api_key(api_key_env)
        if not api_key:
            raise LLMError(f"No API key for the openai backend: set {API_KEY_ENV} (or {api_key_env or DEFAULT_API_KEY_ENV}).")
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url
        )

    async def invoke(self, prompt: str) -> str:
        try:
            response = await self._client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
            )
            return response.choices[0].message.content
        except Exception as e:
            raise LLMError(f"OpenAI call failed: {e}") from e


class OllamaClient(LLMClient):
    def __init__(self, model_name: str, base_url: str = "http://localhost:11434"):
        self.model_name = model_name
        self._client = ollama_client.AsyncClient(host=base_url)

    async def invoke(self, prompt: str) -> str:
        try:
            response = await self._client.chat(
                model=self.model_name,
                messages=[{"role": "user", "content": prompt}],
            )
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
    ):
        #only forward what's set so each client keeps its own defaults
        opts = {"base_url": base_url} if base_url else {}
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
