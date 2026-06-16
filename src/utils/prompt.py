from pathlib import Path

# default path to the framework-level base prompt
DEFAULT_PROMPT_PATH = Path(__file__).parent.parent / "prompts" / "agent_base.md"

class PromptReader:
    """
    Reads prompt templates from .md files as plain strings.
    No markdown parsing — the LLM receives raw text.
    """
    
    @staticmethod
    def read_prompt(path: Path = DEFAULT_PROMPT_PATH) -> str:
        """
        Reads a prompt template file and returns its contents as a string.
        
        Args:
            path: Path to the .md prompt file. Defaults to src/prompts/agent_base.md
        
        Returns:
            Raw string contents of the prompt file.
        
        Raises:
            FileNotFoundError: if the prompt file does not exist at the given path.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Prompt file not found at: {path}. "
                f"Make sure src/prompts/agent_base.md exists."
            )
        return path.read_text(encoding="utf-8")
    
    @staticmethod
    def read_execution_prompt(path: Path) -> str:
        """
        Reads an agent-specific execution prompt.
        Used by agent builders to provide their agent's role and behaviour.
        
        Args:
            path: Path to the agent's execution prompt .md file
        
        Returns:
            Raw string contents of the execution prompt file.
        
        Raises:
            FileNotFoundError: if the file does not exist.
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(
                f"Execution prompt not found at: {path}."
            )
        return path.read_text(encoding="utf-8")