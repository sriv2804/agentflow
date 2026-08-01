from typing import Any, Dict, Optional, Tuple, Literal, List, TYPE_CHECKING


class ScratchPad:
    def __init__(
        self,
        max_len = 20,
        max_tool_grps = 3
    ):
        self.max_len: int = max_len
        self.max_tool_grps = max_tool_grps
        self.trail : List[str] = []
        self.active_skill: Optional[str] = ""
        self.loaded_tool_grp_str : Optional[str] = ""
        self.loaded_tool_grp_list : List[Dict] = []
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
        if self.loaded_tool_grp_str:
            msg_str += f"[LOADED TOOL GROUP]\n\n{self.loaded_tool_grp_str}"
        if self.trail_str:
            msg_str += f"[REASONING TRAIL]\n{self.trail_str}"
        return msg_str
    
    def get_scratchpad_str(self):
        return self.msg_str if self.msg_str else "(empty — this is your first step)"
    
    def evict_and_add_tool_grp(self, tool_grp_name: str, tool_grp_desc):
        self.loaded_tool_grp_list.pop(0)
        self.loaded_tool_grp_list.append(
            {
                "name": tool_grp_name,
                "description": tool_grp_desc
            }
        )
        
    def load_group(self, tool_grp_name: str, tool_grp_desc: str):
        if len(self.loaded_tool_grp_list) >= self.max_tool_grps:
            self.evict_and_add_tool_grp(
                tool_grp_name,
                tool_grp_desc
            )
            self.loaded_tool_grp_str = ""
            for tool_grp_dict in self.loaded_tool_grp_list:
                self.loaded_tool_grp_str += (
                        f"[{tool_grp_dict['name']}]:\n"
                        f"{tool_grp_dict["description"]}"
                )
        else:
            self.loaded_tool_grp_str += (
                f"[{tool_grp_name}]:\n"
                f"{tool_grp_desc}"
            )
        self.msg_str = self._render()
            
        
class MemoryManager:
    def __init__(
        self,
        max_short_term: int = 20
    ):
        self.summary : str = ""
        self.conversation_history : List[Dict] = []
        self.max_short_term = max_short_term
        self.scratchpad = ScratchPad(max_short_term)
        self.working_memory: str = ""   # persistent index of long-term stores
        self._memory_pressure_triggered: bool = False

    def should_trigger_memory_pressure(self) -> bool:
        """Returns True when conversation history hits 70% of max_short_term"""
        threshold = int(self.max_short_term * 0.7)
        return len(self.conversation_history) >= threshold and not self._memory_pressure_triggered

    def mark_pressure_triggered(self):
        self._memory_pressure_triggered = True

    def reset_pressure_flag(self):
        """Reset after eviction so it can trigger again next window"""
        self._memory_pressure_triggered = False

    def append_msg(self, role: str, content: str):
        self.conversation_history.append({
            "role": role,
            "content": content
        })
        if len(self.conversation_history) > self.max_short_term:
            self.conversation_history.pop(0)
            self.reset_pressure_flag()
            
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