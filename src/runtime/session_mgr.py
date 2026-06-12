from typing import Any, Dict, Optional, Tuple
from uuid import uuid4
from dataclasses import dataclass
from src.runtime.channel import AsyncChannel

@dataclass
class AgentRunContext:
    """Request-scoped inputs needed to start an agent task."""

    session_id: str
    channel: AsyncChannel
    
class SessionManager:
    """
    A simply registry for clients' sessions
    Uses session_id as the key
    """
    def __init__(self):
        self.registry: Dict[str, Dict] = {}
        
    def create_session(self) -> Dict[str, AsyncChannel]:
        """
        creates a channel referenced by the provided session_id
        """
        session_id = str(uuid4())
        channel = AsyncChannel()
        self.registry[session_id] = channel
        session = {
            "session_id": session_id,
            "channel": channel
        }
        return session
    
    def delete_session(self, session_id: str) -> None:
        """
        removes the channel referenced by the session_id
        No-OP if session_id does not exist
        """
        
        self.registry.pop(session_id, None)
        return
    
    def get_session(self, session_id: str) -> Optional[AsyncChannel]:
        """
        returns the channel referenced by session_id
        None if not found
        """
        return self.registry.get(session_id, None)