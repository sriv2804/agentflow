"""
Run config (YAML via --config, or CLI flags):

    judge:    {backend, model_name, base_url, api_key_env}   # default: the agents' llm settings
    user_sim: {backend, model_name, base_url, api_key_env}   # default: the judge
    concurrency: 1
    infra_retry: 1
    metadata: {}            # free-form, copied into the manifest (git_sha, label, ...)
    ci_min_pass_rate: null  # optional, 0..1
    output_dir: eval_runs

API keys are never stored here: api_key_env names the variable to read
(AGENTFLOW_API_KEY is tried first, as for the agents).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from src.utils.llm import LLM, llm_settings


class ModelConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    backend: str = "openai"
    model_name: str
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None

    def build(self, temperature: float = 0.0) -> LLM:
        return LLM(model_name=self.model_name, backend=self.backend, base_url=self.base_url,
                   api_key_env=self.api_key_env, temperature=temperature)

    def describe(self) -> Dict[str, Any]:
        """For the manifest: never includes a key, only the env var's name."""
        return {"backend": self.backend, "model_name": self.model_name, "base_url": self.base_url}


class RunConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    judge: Optional[ModelConfig] = None
    user_sim: Optional[ModelConfig] = None
    concurrency: int = Field(1, ge=1)
    infra_retry: int = Field(1, ge=0)
    metadata: Dict[str, Any] = Field(default_factory=dict)
    ci_min_pass_rate: Optional[float] = Field(None, ge=0, le=1)
    output_dir: str = "eval_runs"

    def judge_model(self) -> ModelConfig:
        if self.judge is not None:
            return self.judge
        return default_model_from_agent_config()

    def user_sim_model(self) -> ModelConfig:
        return self.user_sim if self.user_sim is not None else self.judge_model()


def default_model_from_agent_config(config_path: str = "config.yaml") -> ModelConfig:
    """The agents' own llm settings (config.yaml + AGENTFLOW_MODEL_* env vars)."""
    config = {}
    if Path(config_path).exists():
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}
    cfg = llm_settings(config)
    return ModelConfig(backend=cfg.get("backend", "openai"), model_name=cfg.get("model_name") or "",
                       base_url=cfg.get("base_url"), api_key_env=cfg.get("api_key_env"))


class ConfigError(Exception):
    pass


def load_run_config(path: Optional[str] = None, **overrides: Any) -> RunConfig:
    data: Dict[str, Any] = {}
    if path:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            raise ConfigError(f"{path}: the run config must be a YAML mapping")
    data.update({k: v for k, v in overrides.items() if v is not None})
    try:
        return RunConfig.model_validate(data)
    except ValidationError as e:
        lines = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()]
        raise ConfigError(f"invalid run config {path or '(flags)'}:\n" + "\n".join(f"  - {l}" for l in lines))
