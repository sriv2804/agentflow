"""
End-to-end run of the competitive_analysis flow over the real SSE API.

    python -m tests.scripts.competitive_analysis_e2e
    python -m tests.scripts.competitive_analysis_e2e --answer "features and pricing, 20-person eng team"
    python -m tests.scripts.competitive_analysis_e2e --query "Figma vs Canva for a marketing team"
    python -m tests.scripts.competitive_analysis_e2e --base-url http://localhost:8000
    python -m tests.scripts.competitive_analysis_e2e --query "..." --resume <session_id>

Without --base-url, the FastAPI app is started in-process (flow name overridden
to competitive_analysis). HITL clarifications are answered from --answer values
in order, then from stdin.
"""
import argparse
import json
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn
import yaml

FLOW_NAME = "competitive_analysis"
QUERY = "Give me a competitive analysis of Notion vs Linear for engineering teams"
IN_PROCESS_PORT = 8765


def start_in_process_server() -> str:
    from src.sse_server.app import app, config
    config["flow"]["name"] = FLOW_NAME  # routes read this per request

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=IN_PROCESS_PORT, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    while not server.started:
        time.sleep(0.1)
    return f"http://127.0.0.1:{IN_PROCESS_PORT}"


def post_json(url: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload or {}).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def iter_sse(url: str):
    with urllib.request.urlopen(url) as resp:
        for raw in resp:
            line = raw.decode().strip()
            if line.startswith("data: "):
                yield json.loads(line[len("data: "):])


def wait_for_session_exit(session_id: str, timeout: float = 10):
    """In-process server: let the session task finish (it saves history on exit)."""
    from src.sse_server.app import agent_manager
    deadline = time.time() + timeout
    while session_id in agent_manager.registry and time.time() < deadline:
        time.sleep(0.1)


def find_report(session_id: str, started_at: float) -> Path | None:
    with open("config.yaml") as f:
        storage = yaml.safe_load(f).get("storage", {})
    reports_dir = Path(storage.get("working_dir", ".agentflow")) / session_id / "reports"
    reports = [p for p in reports_dir.glob("*.md") if p.stat().st_mtime >= started_at]
    return max(reports, key=lambda p: p.stat().st_mtime) if reports else None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", help="use an already-running server instead of starting one")
    parser.add_argument("--query", default=QUERY, help="initial user query")
    parser.add_argument("--resume", help="session_id of a previous run to resume")
    parser.add_argument("--answer", action="append", default=[], help="scripted HITL answer (repeatable)")
    args = parser.parse_args()

    base_url = args.base_url.rstrip("/") if args.base_url else start_in_process_server()
    scripted_answers = list(args.answer)
    started_at = time.time()

    chat = post_json(f"{base_url}/chats", {"session_id": args.resume} if args.resume else None)
    print(f"session: {chat['session_id']}")
    print(f"\n[user] {args.query}\n")
    post_json(chat["input_url"], {"text": args.query})

    clarifications = 0
    final = None
    for msg in iter_sse(chat["output_stream_url"]):
        msg_type, content = msg.get("message_type", "info"), msg.get("content", "")
        if msg_type == "response":
            clarifications += 1
            print(f"\n[agent asks] {content}")
            if scripted_answers:
                answer = scripted_answers.pop(0)
                print(f"[user] {answer} (scripted)")
            else:
                answer = input("[user] > ")
            post_json(chat["input_url"], {"text": answer})
            print()
        elif msg_type == "done":
            final = content
            print(f"\n[final]\n{content}")
            break
        else:
            print(f"[{msg_type}] {content}")

    if not args.base_url:
        wait_for_session_exit(chat["session_id"])
    report = find_report(chat["session_id"], started_at)
    print("\n" + "=" * 60)
    print(f"HITL clarifications asked: {clarifications}")
    print(f"Report file: {report.resolve() if report else 'NOT FOUND'}")
    print(f"Session id: {chat['session_id']}  (resume with --resume {chat['session_id']})")
    if final is None:
        print("Stream ended without a 'done' message.")


if __name__ == "__main__":
    main()
