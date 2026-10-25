"""Smoke-test the configured LLM: config.yaml `llm` block plus AGENTFLOW_MODEL_* env overrides."""
import asyncio
import yaml
from dotenv import load_dotenv
load_dotenv()

from src.utils.llm import LLM, llm_settings

async def test():
    with open("config.yaml") as f:
        cfg = llm_settings(yaml.safe_load(f))
    llm = LLM(
        model_name=cfg["model_name"],
        backend=cfg.get("backend", "openai"),
        base_url=cfg.get("base_url"),
        api_key_env=cfg.get("api_key_env"),
    )
    print(f"{cfg.get('backend')} / {cfg['model_name']} @ {cfg.get('base_url') or 'default endpoint'}")
    response = await llm.invoke("Say hello in one sentence.")
    print(f"Response: {response}")

asyncio.run(test())
