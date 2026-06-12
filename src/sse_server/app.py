from contextlib import asynccontextmanager
from fastapi import FastAPI
from src.runtime.scheduler import AsyncAgentManager
from src.runtime.session_mgr import SessionManager

agent_manager = AsyncAgentManager()
session_manager = SessionManager()

@asynccontextmanager
async def lifespan(app: FastAPI):
    agent_manager.run()   # starts agent thread before first request
    yield
    # future: graceful shutdown

app = FastAPI(lifespan=lifespan)

# import routes after app is created to avoid circular imports
from src.sse_server import routes  # noqa: E402, F401