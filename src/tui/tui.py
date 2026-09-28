"""
Terminal delivery for agentflow — another consumer of AsyncChannel, alongside
the SSE server. The agent runtime is untouched: the TUI creates its own channel
and agent thread from the same primitives.

    python -m src.tui.tui                      # new session, prompts for your query
    python -m src.tui.tui --resume <session_id>
    python -m src.tui.tui --query "..."        # skip the prompt (scripted runs)

Threads:
  - main thread: asyncio loop that is the channel's client_loop (reads/prints)
  - agent thread: daemon thread running its own loop, the channel's agent_loop
"""
import asyncio
import threading
import time
import uuid
from pathlib import Path

import yaml
from colorama import Fore, Style, just_fix_windows_console
from dotenv import load_dotenv

from src.runtime.channel import AsyncChannel
from src.runtime.run_context import AgentRunContext, run_on_channel

AGENT_COLORS = {
    "orchestrator": Fore.CYAN,
    "researcher": Fore.YELLOW,
    "report_generator": Fore.MAGENTA,
}
USER_STYLE = Fore.WHITE + Style.BRIGHT
TOOL_PREFIX = "invoking tool : "
SHUTDOWN_TIMEOUT = 10  # seconds to let the session save its history on exit


def load_config(path: str = "config.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def start_agent_loop() -> asyncio.AbstractEventLoop:
    """Start a daemon thread running its own event loop; return that loop."""
    ready = threading.Event()
    holder = {}

    def _run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        holder["loop"] = loop
        ready.set()
        loop.run_forever()

    threading.Thread(target=_run, name="agentflow-agent", daemon=True).start()
    ready.wait()
    return holder["loop"]


def paint(agent: str, text: str) -> str:
    return f"{AGENT_COLORS.get(agent, Fore.GREEN)}{text}{Style.RESET_ALL}"


def print_info(agent: str, content: str):
    if content.startswith(TOOL_PREFIX):
        content = f"{Style.DIM}→ tool: {content[len(TOOL_PREFIX):]}{Style.NORMAL}"
    print(paint(agent, f"[{agent}] {content}"), flush=True)


def session_dir(config: dict, session_id: str) -> Path:
    return Path(config.get("storage", {}).get("working_dir", ".agentflow")) / session_id


def find_report(config: dict, session_id: str, started_at: float) -> Path | None:
    reports_dir = session_dir(config, session_id) / "reports"
    reports = [p for p in reports_dir.glob("*.md") if p.stat().st_mtime >= started_at]
    return max(reports, key=lambda p: p.stat().st_mtime).resolve() if reports else None


def read_line(prompt: str) -> asyncio.Future:
    """input() on a daemon thread, so a pending prompt never blocks exit (Ctrl+C)."""
    loop = asyncio.get_running_loop()
    future = loop.create_future()

    def resolve(setter, value):
        if not future.done():
            setter(value)

    def _read():
        try:
            line = input(prompt)
            outcome = (future.set_result, line)
        except BaseException as e:  # EOFError when stdin closes
            outcome = (future.set_exception, e)
        try:
            loop.call_soon_threadsafe(resolve, *outcome)
        except RuntimeError:
            pass  # loop already closed

    threading.Thread(target=_read, daemon=True).start()
    return future


async def prompt_user() -> str:
    while True:
        answer = (await read_line(f"{USER_STYLE}> ")).strip()
        print(Style.RESET_ALL, end="", flush=True)
        if answer:
            print()
            return answer


async def ask_user(agent: str, question: str) -> str:
    print(paint(agent, f"\n[{agent} asks] {question}"), flush=True)
    return await prompt_user()


async def run_tui(query: str | None, flow_name: str, config: dict, session_id: str, resumed: bool) -> int:
    channel = AsyncChannel()
    channel.client_loop = asyncio.get_running_loop()   # same role as the server loop in routes.py
    channel.agent_loop = start_agent_loop()

    run_ctx = AgentRunContext(session_id=session_id, channel=channel, flow_name=flow_name)
    finished = threading.Event()

    async def run_session():
        try:
            await run_on_channel(run_ctx)  # saves conversation history as it exits
        finally:
            finished.set()

    # the session starts first and waits on the channel for the user's query
    run_future = asyncio.run_coroutine_threadsafe(run_session(), channel.agent_loop)
    print(f"{Style.BRIGHT}{'Resumed' if resumed else 'Started'} session{Style.RESET_ALL} {session_id}  "
          f"{Style.DIM}(flow: {flow_name}){Style.RESET_ALL}")
    try:
        return await converse(channel, query, config, session_id, asyncio.wrap_future(run_future))
    finally:
        if not run_future.done():
            run_future.cancel()  # Ctrl+C / stdin closed: cancel so the session still saves
        await asyncio.to_thread(finished.wait, SHUTDOWN_TIMEOUT)
        if session_dir(config, session_id).is_dir():  # nothing to resume if no query ran
            print(f"{Style.BRIGHT}Session id:{Style.RESET_ALL} {session_id}  "
                  f"{Style.DIM}(resume with --resume {session_id}){Style.RESET_ALL}")


async def converse(channel: AsyncChannel, query: str | None, config: dict, session_id: str,
                   flow_done: asyncio.Future) -> int:
    if query:
        print(f"{USER_STYLE}> {query}{Style.RESET_ALL}\n", flush=True)
    else:
        print("What would you like to do? (Ctrl+C to quit)")
        query = await prompt_user()
    started_at = time.time()
    await channel.send_to_agent(query)

    while True:
        receive = asyncio.ensure_future(channel.receive_from_agent())
        done, _ = await asyncio.wait({receive, flow_done}, return_when=asyncio.FIRST_COMPLETED)

        if receive not in done:
            # the flow ended without sending "done" — surface why instead of hanging
            receive.cancel()
            error = flow_done.exception()
            print(f"{Fore.RED}[error] flow stopped without a final answer"
                  f"{f': {error!r}' if error else ''}{Style.RESET_ALL}")
            return 1

        msg = receive.result()
        msg_type = msg.get("message_type", "info")
        agent = msg.get("agent", "agent")
        content = str(msg.get("content", ""))

        if msg_type == "info":
            print_info(agent, content)
        elif msg_type == "response":
            await channel.send_to_agent(await ask_user(agent, content))
        elif msg_type == "done":
            print(f"\n{Style.BRIGHT}[done]{Style.RESET_ALL} {content}")
            report = find_report(config, session_id, started_at)
            if report:
                print(f"{Style.BRIGHT}Report saved to:{Style.RESET_ALL} {report}")
            # let the flow return normally so its history save completes
            await asyncio.wait({flow_done}, timeout=SHUTDOWN_TIMEOUT)
            return 0


def resolve_session_id(resume: str | None, config: dict) -> str:
    if not resume:
        return str(uuid.uuid4())
    try:
        session_id = str(uuid.UUID(resume))  # also keeps path segments out
    except ValueError:
        raise SystemExit(f"--resume expects a session id (UUID), got: {resume}")
    if not session_dir(config, session_id).is_dir():
        raise SystemExit(f"No stored session {session_id} under {session_dir(config, '')}")
    return session_id


def main():
    import argparse
    parser = argparse.ArgumentParser(description="agentflow TUI")
    parser.add_argument("--query", default=None, help="Initial query; skips the prompt (for scripted runs)")
    parser.add_argument("--flow", default=None, help="Flow name (overrides config.yaml)")
    parser.add_argument("--resume", default=None, help="session_id of a previous session to resume")
    args = parser.parse_args()

    load_dotenv()
    just_fix_windows_console()
    config = load_config()
    flow_name = args.flow or config["flow"]["name"]
    session_id = resolve_session_id(args.resume, config)

    try:
        raise SystemExit(asyncio.run(run_tui(args.query, flow_name, config, session_id, bool(args.resume))))
    except (KeyboardInterrupt, EOFError):
        print(f"{Style.RESET_ALL}\n[interrupted]")
        raise SystemExit(130)


if __name__ == "__main__":
    main()
