import asyncio
import json
from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel
from src.sse_server.app import app, agent_manager, session_manager, config
from src.runtime.run_context import AgentRunContext


class UserInput(BaseModel):
    text: str


@app.post("/chats")
async def create_chat(request: Request):
    session = session_manager.create_session()
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
                agent_msg = await channel.receive_from_agent()
                message_type = agent_msg.get("message_type", "info")
                if isinstance(agent_msg, dict):
                    payload = json.dumps(agent_msg)
                else:
                    payload = json.dumps({"message": str(agent_msg)})
                yield f"data: {payload}\n\n"
                if message_type == "done":
                    session_manager.delete_session(session_id)
                    return
        except asyncio.CancelledError:
            print(f"Stream connection closed for {session_id}")

    return StreamingResponse(
        content=event_generator(),
        media_type="text/event-stream"
    )


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
        return JSONResponse(status_code=200)
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
