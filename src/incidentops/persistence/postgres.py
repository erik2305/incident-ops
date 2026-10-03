"""Official asynchronous PostgreSQL checkpointing with strict deserialization."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


@asynccontextmanager
async def open_checkpointer(database_url: str) -> AsyncIterator[AsyncPostgresSaver]:
    """Open one saver lifecycle; connection and configuration errors propagate."""
    serializer = JsonPlusSerializer(
        allowed_msgpack_modules=None,
        pickle_fallback=False,
    )
    async with AsyncPostgresSaver.from_conn_string(
        database_url, serde=serializer
    ) as checkpointer:
        yield checkpointer


async def setup_checkpoints(database_url: str) -> None:
    """Explicitly apply LangGraph's supported, repeatable checkpoint schema setup."""
    async with open_checkpointer(database_url) as checkpointer:
        await checkpointer.setup()
