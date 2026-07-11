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


async def run_session(manager: AsyncAgentManager, query: str, session_id: str):
    channel = AsyncChannel()
    channel.client_loop = asyncio.get_running_loop()
    manager.set_agent_event_loop(channel)

    run_ctx = AgentRunContext(
        session_id=session_id,
        channel=channel,
        flow_name="qa_agent"
    )
    manager.submit(run_ctx)

    await asyncio.sleep(0.5)
    await channel.send_to_agent(query)

    while True:
        msg = await channel.receive_from_agent()
        print(f"[{session_id}][{msg.get('message_type', 'info')}] {msg.get('content', '')}")
        if msg.get('message_type') in ('done', 'error'):
            break


async def test_skill_save_and_retrieve():
    manager = AsyncAgentManager()
    manager.run()

    print("\n=== SESSION 1: Solve task + save skill ===")
    await run_session(
        manager,
        "What is the ratio of India's population to Australia's? After answering, save the flow in a generic manner as a skill for future use.",
        "session-skill-save"
    )

    await asyncio.sleep(1)

    print("\n=== SESSION 2: Same task — should retrieve and reuse skill ===")
    await run_session(
        manager,
        "What is the ratio of China's population to Canada's?Use previously saved skills",
        "session-skill-retrieve"
    )

asyncio.run(test_skill_save_and_retrieve())
