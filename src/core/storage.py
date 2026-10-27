"""
Storage path helpers for agentflow.
All persistent storage lives under:
  {working_dir}/{session_id}/
    {agent_name}_working_memory.json
    session_status.json
    conversation_history_{agent_name}.json
    chromadb/
      skills_{agent_name}/
      recall_{agent_name}/
      facts_{agent_name}/
"""
from pathlib import Path
import yaml


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


def get_session_status_path(working_dir: str, session_id: str) -> Path:
    return get_session_dir(working_dir, session_id) / "session_status.json"


def configured_working_dir(config_path: str = "config.yaml") -> str:
    """storage.working_dir from config.yaml, ".agentflow" if unset or no config."""
    path = Path(config_path)
    if not path.exists():
        return ".agentflow"
    with open(path) as f:
        config = yaml.safe_load(f) or {}
    return (config.get("storage") or {}).get("working_dir", ".agentflow")
