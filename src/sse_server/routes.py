import asyncio
import json
import logging
import os
import uuid
from pathlib import Path
from typing import Optional
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from src.sse_server.app import app, agent_manager, session_manager, config
from src.runtime.run_context import AgentRunContext
from src.runtime.channel import ChannelClosed

logger = logging.getLogger("agentflow.server")

# default text of the final SSE event when the channel closes without a message
CLOSE_TEXT = {
    "done": "Session ended.",
    "error": "The flow stopped because of an error.",
    "shutdown": "The server is shutting down; the session was saved and can be resumed.",
}


class UserInput(BaseModel):
    text: str


class CreateChat(BaseModel):
    session_id: Optional[str] = None   # pass a previous session_id to resume it


def _resumable_session_id(session_id: str) -> str:
    try:
        session_id = str(uuid.UUID(session_id))  # also keeps path segments out
    except ValueError:
        raise HTTPException(status_code=400, detail="session_id must be a UUID")
    if session_manager.get_session(session_id):
        raise HTTPException(status_code=409, detail="Session is already active")
    working_dir = config.get("storage", {}).get("working_dir", ".agentflow")
    if not (Path(working_dir) / session_id).is_dir():
        raise HTTPException(status_code=404, detail="No stored session with this id")
    return session_id


@app.get("/healthz")
async def healthz():
    # liveness only: no LLM call, so probes stay cheap and work when the model is down.
    # version comes from AGENTFLOW_VERSION if set, "dev" otherwise
    return {"status": "ok", "version": os.environ.get("AGENTFLOW_VERSION", "dev")}


@app.post("/chats")
async def create_chat(request: Request, payload: Optional[CreateChat] = None):
    if request.app.state.shutting_down:
        raise HTTPException(
            status_code=503,
            detail="Server is shutting down",
            headers={"Retry-After": "30"},
        )
    resume_id = payload.session_id if payload else None
    if resume_id:
        resume_id = _resumable_session_id(resume_id)
    session = session_manager.create_session(resume_id)
    session_id = session['session_id']
    channel = session['channel']

    channel.client_loop = asyncio.get_running_loop()

    run_ctx = AgentRunContext(
        session_id=session_id,
        channel=channel,
        flow_name=config['flow']['name']
    )
    agent_manager.submit(run_ctx)

    base_url = str(request.base_url).rstrip("/")
    return {
        "session_id": session_id,
        "input_url": f"{base_url}/chats/{session_id}/input",
        "output_stream_url": f"{base_url}/chats/{session_id}/output/stream"
    }


@app.get("/chats/{session_id}/output/stream")
async def stream_output(session_id: str):
    channel = session_manager.get_session(session_id)
    if not channel:
        raise HTTPException(status_code=404, detail="Chat session not found")

    async def event_generator():
        try:
            while True:
                try:
                    agent_msg = await channel.receive_from_agent()
                except ChannelClosed as closed:
                    # the flow ended without (another) final message: done, error or shutdown
                    yield _sse({
                        "message_type": closed.reason,
                        "content": closed.message or CLOSE_TEXT[closed.reason],
                    })
                    session_manager.delete_session(session_id)
                    return
                if isinstance(agent_msg, dict):
                    message_type = agent_msg.get("message_type", "info")
                    yield _sse(agent_msg)
                else:
                    message_type = "info"
                    yield _sse({"message": str(agent_msg)})
                if message_type == "done":
                    session_manager.delete_session(session_id)
                    return
        except asyncio.CancelledError:
            # client went away mid-stream: the flow keeps running (no idle-session cleanup yet)
            logger.info("stream connection closed for %s", session_id)

    return StreamingResponse(
        content=event_generator(),
        media_type="text/event-stream"
    )


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


@app.post("/chats/{session_id}/input")
async def send_input(session_id: str, payload: UserInput):
    channel = session_manager.get_session(session_id)
    if not channel:
        return JSONResponse(
            status_code=404,
            content={"error": "session does not exist"}
        )
    success = await channel.send_to_agent(payload.text)
    if success:
        return JSONResponse(status_code=200, content={"status": "ok"})
    return JSONResponse(
        status_code=400,
        content={
            "error": (
                "Not your turn. Please wait for the agent to "
                "finish processing."
            )
        }
    )


@app.delete("/chats/{session_id}")
async def delete_session(session_id: str):
    agent_manager.cancel(session_id)
    session_manager.delete_session(session_id)
    return {"status": "deleted", "session_id": session_id}
