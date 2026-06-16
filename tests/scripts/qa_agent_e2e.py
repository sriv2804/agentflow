import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

from src.runtime.channel import AsyncChannel
from src.runtime.scheduler import AsyncAgentManager
from src.runtime.run_context import AgentRunContext, run_on_channel

async def test_e2e():
    # start agent manager
    manager = AsyncAgentManager()
    manager.run()

    # create channel
    channel = AsyncChannel()
    channel.client_loop = asyncio.get_running_loop()
    manager.set_agent_event_loop(channel)

    # create run context and submit
    run_ctx = AgentRunContext(
        session_id="test-session-001",
        channel=channel,
        flow_name="qa_agent"
    )
    manager.submit(run_ctx)

    # send first user message
    await asyncio.sleep(0.5)  # give agent loop time to start
    await channel.send_to_agent("What is the current population of India? "
    "Divide it by the population of Australia and tell me the ratio.")

    # drain responses until done
    while True:
        msg = await channel.receive_from_agent()
        print(f"[{msg.get('message_type', 'info')}] {msg.get('content', '')}")
        if msg.get('message_type') in ('done', 'error', 'response'):
            break

asyncio.run(test_e2e())