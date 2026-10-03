"""Explicit schema provisioning through the existing checkpoint helper."""

import asyncio
import os

from incidentops.persistence import setup_checkpoints


def main():
    database_url = os.environ.get("INCIDENTOPS_DATABASE_URL")
    if not database_url:
        raise ValueError("INCIDENTOPS_DATABASE_URL is required")
    # Psycopg's asynchronous connection requires a selector loop on Windows.
    with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
        runner.run(setup_checkpoints(database_url))


if __name__ == "__main__":
    main()
