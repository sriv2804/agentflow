

class LLM:
    def __init__(self, model_name):
        self.model_name = model_name
        
    async def invoke(self, query : str):
        pass