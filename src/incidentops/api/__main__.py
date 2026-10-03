"""Loopback demo server with a Windows-compatible async PostgreSQL loop."""

import asyncio

import uvicorn

from incidentops.api.app import create_app


def main():
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(),
            host="127.0.0.1",
            port=8010,
            http="h11",
            ws="none",
        )
    )
    # Uvicorn's default Windows loop can be incompatible with async Psycopg.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(server.serve())


if __name__ == "__main__":
    main()
