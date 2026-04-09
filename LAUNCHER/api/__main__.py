"""Entry point for the API server: python -m LAUNCHER.api"""

import argparse
import os

import uvicorn

from LAUNCHER.api.app import create_app

app = create_app()


def main(host: str | None = None, port: int | None = None):
    parser = argparse.ArgumentParser(
        prog="python -m LAUNCHER.api",
        description="Slippi AI Launcher API server",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("SLIPPI_API_HOST", "127.0.0.1"),
        help="Bind address (default: 127.0.0.1; use 0.0.0.0 to accept "
             "connections from another host, e.g. a Windows frontend talking "
             "to a backend running inside WSL).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SLIPPI_API_PORT", "8000")),
        help="Listen port (default: 8000).",
    )
    args = parser.parse_args()

    final_host = host if host is not None else args.host
    final_port = port if port is not None else args.port
    uvicorn.run(app, host=final_host, port=final_port)


if __name__ == "__main__":
    main()
