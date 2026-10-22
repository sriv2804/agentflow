import hmac
import os
import sys
import yaml
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from src.runtime.scheduler import AsyncAgentManager
from src.runtime.session_mgr import SessionManager
from src.utils.llm import llm_settings, check_llm_settings
from dotenv import load_dotenv

def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(config_path) as f:
        return yaml.safe_load(f)

load_dotenv() 
config = load_config()
try:
    check_llm_settings(llm_settings(config))
except ValueError as e:
    sys.exit(f"agentflow: startup failed: {e}")
agent_manager = AsyncAgentManager()
session_manager = SessionManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    agent_manager.run()
    yield

app = FastAPI(lifespan=lifespan)


@app.middleware("http")
async def require_bearer_token(request: Request, call_next):
    # auth is opt-in: without AGENTFLOW_AUTH_TOKEN (local runs, kind) every route stays open
    token = os.environ.get("AGENTFLOW_AUTH_TOKEN")
    if token and request.url.path != "/healthz":
        expected = f"Bearer {token}".encode()
        given = request.headers.get("authorization", "").encode()
        if not hmac.compare_digest(given, expected):
            return JSONResponse(
                {"detail": "Missing or invalid bearer token"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)

from src.sse_server import routes  # noqa: E402, F401
