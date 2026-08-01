"""
FactStore: agent-curated facts persisted across sessions.
Backed by ChromaDB, scoped per agent.
Collection name: facts_{agent_name}
"""
from typing import List, Dict
from datetime import datetime, timezone
import uuid
import asyncio
import chromadb

_chroma_client = chromadb.PersistentClient(path=".chromadb")


class FactStore:
    def __init__(self, agent_name: str):
        self.agent_name: str = agent_name
        self.collection = _chroma_client.get_or_create_collection(
            name=f"facts_{agent_name}"
        )

    async def add(self, content: str, source_session: str) -> str:
        fact_id = f"fact_{uuid.uuid4()}"
        created_at = datetime.now(timezone.utc).isoformat()
        await asyncio.to_thread(
            self.collection.upsert,
            ids=[fact_id],
            documents=[content],
            metadatas=[{
                "created_at": created_at,
                "source_session": source_session
            }]
        )
        return fact_id

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
        ids = result["ids"][0]
        documents = result["documents"][0]
        metadatas = result["metadatas"][0]
        for fact_id, document, metadata in zip(ids, documents, metadatas):
            results.append({
                "fact_id": fact_id,
                "content": document,
                "created_at": metadata["created_at"]
            })
        return results

    async def get_all(self) -> List[Dict]:
        existing = await asyncio.to_thread(self.collection.get)
        return [
            {
                "fact_id": fact_id,
                "content": document,
                "created_at": metadata["created_at"]
            }
            for fact_id, document, metadata in zip(
                existing["ids"], existing["documents"], existing["metadatas"]
            )
        ]
