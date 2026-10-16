import pytest

from src.utils.llm import check_llm_settings, llm_settings, resolve_api_key

CONFIG = {"llm": {"backend": "ollama", "model_name": "gemma", "base_url": "http://ollama:11434"}}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("AGENTFLOW_MODEL_BACKEND", "AGENTFLOW_MODEL_NAME", "AGENTFLOW_API_KEY", "GITHUB_TOKEN"):
        monkeypatch.delenv(name, raising=False)


def test_config_yaml_is_default():
    assert llm_settings(CONFIG) == CONFIG["llm"]


def test_env_overrides_backend_and_model(monkeypatch):
    monkeypatch.setenv("AGENTFLOW_MODEL_BACKEND", "openai")
    monkeypatch.setenv("AGENTFLOW_MODEL_NAME", "gpt-4o-mini")
    # ollama's base_url must not leak into the openai backend
    assert llm_settings(CONFIG) == {"backend": "openai", "model_name": "gpt-4o-mini"}


def test_api_key_prefers_agentflow_then_github_token(monkeypatch):
    assert resolve_api_key() is None
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    assert resolve_api_key() == "gh"
    monkeypatch.setenv("AGENTFLOW_API_KEY", "af")
    assert resolve_api_key() == "af"


def test_missing_key_fails_with_clear_message():
    with pytest.raises(ValueError, match="AGENTFLOW_API_KEY"):
        check_llm_settings({"backend": "openai", "model_name": "gpt-4o-mini"})
    check_llm_settings(CONFIG["llm"])  # ollama needs no key
