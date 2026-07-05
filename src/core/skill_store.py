from typing import Any, List, Optional, Dict
from pathlib import Path
import chromadb
import yaml

def _load_config(path : str = "config.yaml") ->dict:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found {path}")
    with open(config_path) as f:
        return yaml.safe_load(f)

_chroma_client = chromadb.PersistentClient(path=".chromadb")
class SkillStore:
    def __init__(
        self,
        agent_name
    ):
        self.agent_name : str = agent_name
        config = _load_config()
        self.agent_skills_path : Path = Path(config['base_skill_path'])/f"{self.agent_name}_skills"
        Path(self.agent_skills_path).mkdir(parents = True, exist_ok = True)
        self.collection = _chroma_client.get_or_create_collection(
            name=f"{agent_name}_skills"
        )
        existing = self.collection.get()
        self.skill_list = existing["ids"] if existing["ids"] else []
    
    def add(self, skill_dict : dict):
        #we expect the skill_dict to be well formatted , this will be the responsibilty
        #of the add_skill tool , if any field is missing it should raise an exception
        skill_name = skill_dict.get("name", "")
        skill_description = skill_dict.get("description", "")
        skill_content = skill_dict.get("content", "")
        self.skill_list.append(skill_name)
        self._write_content(skill_content, skill_name)
        self.collection.add(
            ids = [f"{skill_name}"],
            documents = [skill_description],
            metadatas = [{
                "skill_name": skill_name
            }]
        )
    
    def _write_content(self, content: str, file_name: str):
        with open(self.agent_skills_path/f"{file_name}.md", mode='w') as f:
            f.write(content)
    
    def _read_content(self, file_name: str):
        skill_path = self.agent_skills_path/f"{file_name}.md"
        if not skill_path.exists():
            raise FileNotFoundError(f"Skill file not found : {str(skill_path)}")
        skill_content = ""
        with open(skill_path) as f:
            skill_content = f.read()
        return skill_content
    
    def query(self, query_str : str):
        if self.collection.count() == 0:
           return None
        result = self.collection.query(
            query_texts=[query_str],
            n_results=1
        )
        top_id = result["ids"][0][0]
        top_document = result["documents"][0][0]
        top_metadata = result["metadatas"][0][0]
        skill_path = top_metadata["skill_name"]
        skill_content = self._read_content(skill_path)
        return skill_content
    
    def list_all_skills(self) -> List:
        #the idea here is that skill_retriever will have two modes:
        #VIEW_SKILLS: fetches available skills names for the agent(like email_formatting.md, etc)
        #GET_SKILL: gets the skill_content by searching up in ChromaDB expects a task description which is the query_str
        return self.skill_list
        
        