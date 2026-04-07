"""Entry point for the API server: python -m LAUNCHER.api"""

import uvicorn

from LAUNCHER.api.app import create_app

app = create_app()


def main(host: str = "127.0.0.1", port: int = 8000):
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
