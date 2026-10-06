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


class OpenAIClient(LLMClient):
    def __init__(
        self,
        model_name: str,
        base_url: str = "https://models.inference.ai.azure.com",
        api_key_env: str = "GITHUB_TOKEN",
    ):
        self.model_name = model_name
        self._client = AsyncOpenAI(
            api_key=os.environ[api_key_env],
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
