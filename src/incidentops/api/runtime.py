"""Explicit API configuration and request-owned dependency lifetimes."""

import os
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from incidentops.graph.runtime import IncidentRuntimeContext
from incidentops.llm.openrouter import open_openrouter_reasoner
from incidentops.mcp.client import open_read_capabilities, open_write_capabilities


class APIConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    database_url: str = Field(min_length=1, repr=False)
    observability_mcp_url: str = Field(min_length=1, repr=False)
    operations_mcp_url: str = Field(min_length=1, repr=False)
    openrouter_api_key: SecretStr = Field(repr=False)
    openrouter_model: str = Field(min_length=1)

    @field_validator(
        "database_url",
        "observability_mcp_url",
        "operations_mcp_url",
        "openrouter_model",
    )
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Runtime configuration must not be blank")
        return value

    @classmethod
    def from_environment(cls):
        names = {
            "database_url": "INCIDENTOPS_DATABASE_URL",
            "observability_mcp_url": "INCIDENTOPS_OBSERVABILITY_MCP_URL",
            "operations_mcp_url": "INCIDENTOPS_OPERATIONS_MCP_URL",
            "openrouter_api_key": "OPENROUTER_API_KEY",
            "openrouter_model": "INCIDENTOPS_OPENROUTER_MODEL",
        }
        values = {field: os.environ.get(name, "") for field, name in names.items()}
        missing = [name for field, name in names.items() if not values[field].strip()]
        if missing:
            raise ValueError("Missing API configuration: " + ", ".join(missing))
        return cls(**values)


@dataclass(frozen=True)
class RuntimeFactories:
    """Injectable existing factories; no stored clients or new capability surface."""

    reads: Callable = open_read_capabilities
    writes: Callable = open_write_capabilities
    reasoner: Callable = open_openrouter_reasoner

    @asynccontextmanager
    async def start(self, config: APIConfig):
        async with (
            self.reads(
                config.observability_mcp_url, config.operations_mcp_url
            ) as reads,
            self.reasoner(
                api_key=config.openrouter_api_key.get_secret_value(),
                model=config.openrouter_model,
            ) as reasoner,
        ):
            yield IncidentRuntimeContext(read_capabilities=reads, reasoner=reasoner)

    @asynccontextmanager
    async def approval(self, config: APIConfig, decision: str):
        if decision == "reject":
            yield None
            return
        async with (
            self.reads(
                config.observability_mcp_url, config.operations_mcp_url
            ) as reads,
            self.writes(config.operations_mcp_url) as writes,
        ):
            yield IncidentRuntimeContext(
                read_capabilities=reads, write_capabilities=writes
            )
