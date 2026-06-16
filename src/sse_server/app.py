import yaml
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from src.runtime.scheduler import AsyncAgentManager
from src.runtime.session_mgr import SessionManager
from dotenv import load_dotenv

def load_config(path: str = "config.yaml") -> dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")
    with open(config_path) as f:
        return yaml.safe_load(f)

load_dotenv() 
config = load_config()
agent_manager = AsyncAgentManager()
session_manager = SessionManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    agent_manager.run()
    yield

app = FastAPI(lifespan=lifespan)

from src.sse_server import routes  # noqa: E402, F401
