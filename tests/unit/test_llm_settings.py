import pytest

from src.utils.llm import check_llm_settings, llm_settings, resolve_api_key

CONFIG = {"llm": {"backend": "ollama", "model_name": "gemma", "base_url": "http://ollama:11434"}}
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/"
OPENAI_CFG = {"backend": "openai", "model_name": "gemini-3.1-flash-lite", "base_url": GEMINI_URL}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "AGENTFLOW_MODEL_BACKEND", "AGENTFLOW_MODEL_NAME", "AGENTFLOW_MODEL_BASE_URL",
        "AGENTFLOW_API_KEY", "GITHUB_TOKEN", "MY_KEY",
    ):
        monkeypatch.delenv(name, raising=False)


def test_config_yaml_is_default():
    assert llm_settings(CONFIG) == CONFIG["llm"]


def test_env_overrides_backend_and_model(monkeypatch):
    monkeypatch.setenv("AGENTFLOW_MODEL_BACKEND", "openai")
    monkeypatch.setenv("AGENTFLOW_MODEL_NAME", "gemini-3.1-flash-lite")
    # ollama's base_url must not leak into the openai backend
    assert llm_settings(CONFIG) == {"backend": "openai", "model_name": "gemini-3.1-flash-lite"}


def test_env_vars_fully_describe_a_backend(monkeypatch):
    monkeypatch.setenv("AGENTFLOW_MODEL_BACKEND", "openai")
    monkeypatch.setenv("AGENTFLOW_MODEL_NAME", "gemini-3.1-flash-lite")
    monkeypatch.setenv("AGENTFLOW_MODEL_BASE_URL", GEMINI_URL)
    assert llm_settings(CONFIG) == OPENAI_CFG


def test_base_url_env_overrides_ollama_endpoint(monkeypatch):
    monkeypatch.setenv("AGENTFLOW_MODEL_BASE_URL", "http://host.docker.internal:11434")
    assert llm_settings(CONFIG)["base_url"] == "http://host.docker.internal:11434"


def test_api_key_prefers_agentflow_then_configured_env(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "gh")
    assert resolve_api_key() is None  # no implicit GITHUB_TOKEN fallback any more
    monkeypatch.setenv("MY_KEY", "mine")
    assert resolve_api_key("MY_KEY") == "mine"
    monkeypatch.setenv("AGENTFLOW_API_KEY", "af")
    assert resolve_api_key("MY_KEY") == "af"


def test_missing_key_fails_with_clear_message():
    with pytest.raises(ValueError, match="AGENTFLOW_API_KEY"):
        check_llm_settings(OPENAI_CFG)
    check_llm_settings(CONFIG["llm"])  # ollama needs no key


def test_missing_endpoint_fails_with_clear_message(monkeypatch):
    monkeypatch.setenv("AGENTFLOW_API_KEY", "af")
    with pytest.raises(ValueError, match="AGENTFLOW_MODEL_BASE_URL"):
        check_llm_settings({"backend": "openai", "model_name": "gemini-3.1-flash-lite"})
    check_llm_settings(OPENAI_CFG)
