"""Run-scoped OpenRouter structured output, with no executable tool bindings."""

from contextlib import asynccontextmanager

from httpx import HTTPError
from langchain_core.exceptions import OutputParserException
from langchain_openrouter import ChatOpenRouter
from openrouter.errors import OpenRouterError

from incidentops.domain.models import EvidenceItem, Service
from incidentops.domain.reasoning import Assessment, AssessmentSchema, ReasoningError
from incidentops.llm.prompts import build_messages


class OpenRouterReasoner:
    def __init__(self, structured_model):
        self._structured_model = structured_model

    async def assess(
        self,
        *,
        user_report: str,
        target_service: Service,
        evidence: list[EvidenceItem],
        evidence_round: int,
    ) -> Assessment:
        messages = build_messages(
            user_report=user_report,
            target_service=target_service,
            evidence=evidence,
            evidence_round=evidence_round,
        )
        try:
            result = await self._structured_model.ainvoke(messages)
            parsed = AssessmentSchema.model_validate(result)
        except (
            OpenRouterError,
            HTTPError,
            OutputParserException,
            ValueError,
            RuntimeError,
        ):
            # This boundary covers provider/SDK/parser errors, never incident conclusions.
            # Cancellation (BaseException) propagates; external payloads never enter state.
            raise ReasoningError(
                "OpenRouter assessment or structured parsing failed"
            ) from None
        return parsed.model_dump(mode="json")


@asynccontextmanager
async def open_openrouter_reasoner(*, api_key: str, model: str):
    """Create one explicitly configured model and close its owned SDK transports."""
    if not api_key or not isinstance(model, str) or not model.strip():
        raise ValueError("Explicit OpenRouter API key and model are required")
    chat = ChatOpenRouter(
        api_key=api_key,
        model=model,
        # GPT-6 Luna endpoints do not advertise temperature support. A fixed seed
        # retains low variance without conflicting with require_parameters=True.
        temperature=None,
        seed=0,
        max_retries=0,
        timeout=60_000,
        reasoning={"effort": "medium", "exclude": True},
        openrouter_provider={"allow_fallbacks": False, "require_parameters": True},
        app_title=None,
        app_url=None,
        session_id=None,
    )
    # With attribution headers disabled the SDK owns both clients and closes them.
    with chat.client:
        async with chat.client:
            structured = chat.with_structured_output(
                AssessmentSchema,
                method="json_schema",
                strict=True,
                include_raw=False,
                retries=None,  # Explicitly disable the underlying SDK's default retries.
            )
            yield OpenRouterReasoner(structured)
