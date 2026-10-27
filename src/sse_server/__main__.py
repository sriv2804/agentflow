"""
Start the SSE server with graceful shutdown:

    python -m src.sse_server [--host 0.0.0.0] [--port 8000]

Host and port default to server.host / server.port in config.yaml.
"""
import argparse
import logging

import uvicorn

from src.sse_server.app import app, config
from src.sse_server.server import AgentflowServer


def main() -> None:
    server_cfg = config.get("server") or {}
    parser = argparse.ArgumentParser(description="agentflow SSE server")
    parser.add_argument("--host", default=server_cfg.get("host", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(server_cfg.get("port", 8000)))
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
    AgentflowServer(uvicorn.Config(app, host=args.host, port=args.port)).run()


if __name__ == "__main__":
    main()
