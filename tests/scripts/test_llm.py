import asyncio
import os
from dotenv import load_dotenv
load_dotenv()

from src.utils.llm import LLM

async def test():
    llm = LLM(model_name="gpt-4o-mini", backend="openai")
    response = await llm.invoke("Say hello in one sentence.")
    print(f"Response: {response}")

asyncio.run(test())