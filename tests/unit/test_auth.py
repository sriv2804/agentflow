import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    # default config.yaml backend needs no key; keep env overrides from leaking in
    monkeypatch.delenv("AGENTFLOW_MODEL_BACKEND", raising=False)
    monkeypatch.delenv("AGENTFLOW_MODEL_NAME", raising=False)
    from src.sse_server.app import app
    import src.sse_server.routes  # noqa: F401  registers the routes

    return TestClient(app)


def test_no_token_configured_leaves_routes_open(client, monkeypatch):
    monkeypatch.delenv("AGENTFLOW_AUTH_TOKEN", raising=False)

    # unknown session -> 404 from the route itself, i.e. the request got through
    assert client.get("/chats/does-not-exist/output/stream").status_code == 404


def test_missing_or_wrong_token_is_rejected(client, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_AUTH_TOKEN", "s3cret")

    missing = client.get("/chats/does-not-exist/output/stream")
    wrong = client.get("/chats/does-not-exist/output/stream", headers={"Authorization": "Bearer nope"})

    assert missing.status_code == 401
    assert missing.headers["www-authenticate"] == "Bearer"
    assert wrong.status_code == 401


def test_correct_token_is_accepted(client, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_AUTH_TOKEN", "s3cret")

    response = client.get("/chats/does-not-exist/output/stream", headers={"Authorization": "Bearer s3cret"})

    assert response.status_code == 404


def test_healthz_needs_no_token(client, monkeypatch):
    monkeypatch.setenv("AGENTFLOW_AUTH_TOKEN", "s3cret")

    assert client.get("/healthz").status_code == 200
