import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Optional, Tuple

from src.runtime.channel import AsyncChannel

class AsyncAgentManager:
    """
    A async task manager to create/cancel tasks on the agent loop.
    Runs a dedicated thread to service agent tasks
    """
    def __init__(self):
        self.registry: Dict[str, Tuple[Any, asyncio.Task]] = {}
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
    
    def run(self):
        if self._loop is not None:
            return
        start_event = threading.Event()
        
        def _run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            custom_pool = ThreadPoolExecutor(max_workers=200)
            loop.set_default_executor(custom_pool)
            self._loop = loop
            start_event.set()
            try:
                loop.run_forever()
            finally:
                loop.run_until_complete(loop.shutdown_asyncgens())
                loop.close()
            
        self._thread = threading.Thread(target=_run, daemon=True)
        self._thread.start()
        start_event.wait()
        
    async def _create_task(self, run_ctx):
        task = asyncio.create_task(run_on_channel(run_ctx))
        self.registry[run_ctx.chat_id] = (run_ctx.channel, task)
        task.add_done_callback(lambda t : self.registry.pop(run_ctx.chat_id, None))
    
    def submit(self, run_ctx):
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("Agent Event loop is not running, call run() firsts")
        
        self.set_agent_event_loop(run_ctx.channel)
        coro = self._create_task(run_ctx)
        fut = asyncio.run_coroutine_threadsafe(
            coro= coro,
            loop= self._loop
        )
        return fut
    
    async def _cancel(self, chat_id: str):
        if chat_id not in self.registry:
            return
        _, task = self.registry[chat_id]
        
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self.registry.pop(chat_id, None)
        return
    
    def cancel(self, chat_id: str):
        if not self._loop or not self._loop.is_running():
            raise RuntimeError("Agent Event loop is not running")
        coro = self._cancel(chat_id)
        fut = asyncio.run_coroutine_threadsafe(
            coro,
            self._loop
        )
        return fut
    
    def set_agent_event_loop(self, channel: AsyncChannel):
        channel.agent_loop = self._loop
    
    def get_agent_event_loop(self):
        return self._loop
    
    def set_client_event_loop(self, channel: AsyncChannel, client_loop: asyncio.AbstractEventLoop):
        channel.client_loop = client_loop
           