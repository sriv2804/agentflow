from typing import Any, Dict, Optional, Tuple, Literal, List, TYPE_CHECKING


class ScratchPad:
    def __init__(
        self,
        max_len = 20
    ):
        self.max_len: int = max_len
        self.active_skill: Optional[str] = ""
        self.trail : List[str] = []
        self.trail_str: str = ""
        self.msg_str: str  = ""
        
    def _render_trail_str(self):
        self.trail_str = ""
        for trail_msg in self.trail:
            self.trail_str += f"----{trail_msg}----\n"
        return self.trail_str
    
    def append_trail(self, trail_msg: str):
        if len(self.trail) >= self.max_len:
            self.trail_str = ""
            self.trail.pop(0)
            self.trail.append(trail_msg)
            self.trail_str = self._render_trail_str()
            self.msg_str = self._render()
        else:
            self.trail.append(trail_msg)
            self.trail_str += f"----{trail_msg}----\n"
            if len(self.trail) == 1:
                self.msg_str += f"[REASONING TRAIL]\n{self.trail_str}"
            else:
                self.msg_str += f"----{trail_msg}----\n"
            
    def set_skill(self, skill:str):
        self.active_skill = skill
        self.msg_str = self._render()
        
    def _render(self):
        msg_str = ""
        if self.active_skill:
            msg_str += f"[ACTIVE SKILL]\n{self.active_skill}\n"
        if self.trail_str:
            msg_str += f"[REASONING TRAIL]\n{self.trail_str}"
        return msg_str
    
    def get_scratchpad_str(self):
        return self.msg_str if self.msg_str else "(empty — this is your first step)"
            
    
class MemoryManager:
    def __init__(
        self,
        max_short_term: int = 20
    ):
        self.summary : str = ""
        self.conversation_history : List[Dict] = []
        self.max_short_term = max_short_term
        self.scratchpad = ScratchPad(max_short_term) 
    
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