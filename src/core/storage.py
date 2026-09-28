"""
Storage path helpers for agentflow.
All persistent storage lives under:
  {working_dir}/{session_id}/
    {agent_name}_working_memory.json
    conversation_history_{agent_name}.json
    chromadb/
      skills_{agent_name}/
      recall_{agent_name}/
      facts_{agent_name}/
"""
from pathlib import Path


def get_session_dir(working_dir: str, session_id: str) -> Path:
    path = Path(working_dir) / session_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_chromadb_path(working_dir: str, session_id: str) -> Path:
    path = get_session_dir(working_dir, session_id) / "chromadb"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_working_memory_path(working_dir: str, session_id: str, agent_name: str) -> Path:
    session_dir = get_session_dir(working_dir, session_id)
    return session_dir / f"{agent_name}_working_memory.json"


def get_conversation_history_path(working_dir: str, session_id: str, agent_name: str) -> Path:
    session_dir = get_session_dir(working_dir, session_id)
    return session_dir / f"conversation_history_{agent_name}.json"
