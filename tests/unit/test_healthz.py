from fastapi.testclient import TestClient


def test_healthz(monkeypatch):
    # default config.yaml backend needs no key; keep env overrides from leaking in
    monkeypatch.delenv("AGENTFLOW_MODEL_BACKEND", raising=False)
    monkeypatch.delenv("AGENTFLOW_MODEL_NAME", raising=False)
    from src.sse_server.app import app
    import src.sse_server.routes  # noqa: F401  registers the routes

    response = TestClient(app).get("/healthz")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
