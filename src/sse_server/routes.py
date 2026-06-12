import asyncio
import json
from configparser import ConfigParser
from fastapi import HTTPException, Request, Query
from fastapi.responses import StreamingResponse, JSONResponse, HTMLResponse
from pydantic import BaseModel
from src.runtime.scheduler import AsyncAgentManager
from src.runtime.session_mgr import SessionManger, AgentRunContext
from src.sse_server.app import app, agent_manager, session_manager

# Data Model for Input
class UserInput(BaseModel):
    """
    Pydantic model representing the user's input payload.
    """
    text: str
    

@app.post("/chats")
async def create_chat(request: Request):
    session = session_manager.create_session()
    run_ctx = AgentRunContext(
        session_id= session['session_id'],
        channel= session['channel']
    )
    agent_manager.set_client_event_loop(
        channel= session['channel'],
        client_loop= asyncio.get_event_loop()
    )
    agent_manager.submit(run_ctx)
    base_url = str(request.base_url).rstrip("/")
    
    return {
        "session_id" : session['session_id'],
        "input_url" : f"{base_url}/chats/{session['session_id']}/input",
        "output_stream_url" : f"{base_url}/chats/{session['session_id']}/output/stream"
    }
    
@app.get("/chats/{session_id}/output/stream")
async def stream_output(session_id : str):
    session = session_manager.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Chat session not found")
    channel = session['channel']
   
    
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
        pass
    return StreamingResponse(
        content= event_generator(),
        media_type= "text/event-stream"
    )
    
@app.post("/chats/{session_id}/input")
async def send_input(session_id : str, payload: UserInput):
    session = session_manager.get_session(session_id)
    if not session:
         return JSONResponse(
             status_code= 404,
             content= {
                 "error": "session does not exist"
             }
         )
    channel = session['channel']
    success = await channel.send_to_agent(payload.text)
    if success:
        return JSONResponse(
            status_code= 200
        )
    return JSONResponse(
       status_code=400,
       content= {
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
    
    
