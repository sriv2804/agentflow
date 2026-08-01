"""
RecallStore: append-only semantic search over conversation history.
Backed by ChromaDB, scoped per agent.
Collection name: recall_{agent_name}
"""
from typing import List, Dict
from datetime import datetime, timezone
import asyncio
import chromadb

_chroma_client = chromadb.PersistentClient(path=".chromadb")


class RecallStore:
    def __init__(self, agent_name: str):
        self.agent_name: str = agent_name
        self.collection = _chroma_client.get_or_create_collection(
            name=f"recall_{agent_name}"
        )

    async def append(self, session_id: str, turn_id: int, role: str, content: str):
        timestamp = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(
            self.collection.upsert,
            ids=[f"{session_id}_{turn_id}"],
            documents=[f"{role.upper()}: {content}"],
            metadatas=[{
                "session_id": session_id,
                "turn_id": turn_id,
                "role": role,
                "timestamp": timestamp
            }]
        )

    async def search(self, query: str, n_results: int = 5) -> List[Dict]:
        if self.collection.count() == 0:
            return []
        n_results = min(n_results, self.collection.count())
        result = await asyncio.to_thread(
            self.collection.query,
            query_texts=[query],
            n_results=n_results
        )
        results = []
        metadatas = result["metadatas"][0]
        documents = result["documents"][0]
        for metadata, document in zip(metadatas, documents):
            results.append({
                "role": metadata["role"],
                "content": document.split(": ", 1)[-1],
                "session_id": metadata["session_id"],
                "timestamp": metadata["timestamp"]
            })
        return results

    async def get_recent(self, n: int = 10) -> List[Dict]:
        existing = await asyncio.to_thread(self.collection.get)
        entries = list(zip(existing["ids"], existing["documents"], existing["metadatas"]))
        entries.sort(key=lambda e: e[2]["timestamp"])
        recent = entries[-n:]
        return [
            {
                "role": metadata["role"],
                "content": document.split(": ", 1)[-1],
                "session_id": metadata["session_id"],
                "timestamp": metadata["timestamp"]
            }
            for _id, document, metadata in recent
        ]
