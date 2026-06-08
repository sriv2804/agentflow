from typing import Any, Dict, Optional, Tuple, Literal, List, TYPE_CHECKING

class MemoryManager:
    def __init__(
        self,
        max_short_term: int = 20
    ):
        self.summary : str = ""
        self.conversation_history : List[Dict] = []
        self.max_short_term = max_short_term
    
    def append_msg(self, role: str, content: str):
        self.conversation_history.append({
            "role": role,
            "content": content
        })
        if len(self.conversation_history) > self.max_short_term:
            self.conversation_history.pop(0)
            
    def get_messages(self, as_string = True):
        base = [{"role": "system", "content": self.summary}] if self.summary else []
        msg_list = base + self.conversation_history
        msg_str = "CONVERSATION_HISTORY:\n"
        for msg in msg_list:
            msg_str += f"ROLE : {msg['role']} CONTENT : {msg['content']}\n"
        if as_string:
            return msg_str
        return msg_list
    
    def flush_to_corpus(self):
        #upsert to ChromaDB-> how to structure the corpus, what model etc
        pass
    
    def update_summary(self):
        pass