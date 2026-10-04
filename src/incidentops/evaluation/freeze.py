"""Reasoning-surface SHA-256 and non-overwriting holdout freeze enforcement."""

import hashlib
import inspect
import json
from datetime import UTC, datetime

from incidentops.domain import reasoning
from incidentops.evaluation.scenarios import KNOWN
from incidentops.graph import nodes
from incidentops.llm import openrouter, prompts


def reasoning_fingerprint(model, *, prompt=None, schema=None, configuration=None):
    surface = {
        "model": model,
        "system_prompt": prompts.SYSTEM_PROMPT if prompt is None else prompt,
        "assessment_schema": reasoning.AssessmentSchema.model_json_schema()
        if schema is None
        else schema,
        # Hash the actual request construction, including all model/provider settings,
        # structured-output options, untrusted message framing and graph read policy.
        "configuration": {
            "adapter": inspect.getsource(openrouter.open_openrouter_reasoner),
            "messages": inspect.getsource(prompts.build_messages),
            "validation": inspect.getsource(reasoning.validate_assessment),
            "graph_policy": inspect.getsource(nodes),
        }
        if configuration is None
        else configuration,
    }
    return hashlib.sha256(
        json.dumps(
            surface, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def write_freeze(path, model, fingerprint, dev_result):
    if (
        dev_result["provider"] != "live"
        or dev_result["split"] != "dev"
        or not dev_result["complete"]
        or len(dev_result["runs"]) != 12
    ):
        raise ValueError("Freeze requires a completed twelve-attempt live dev result")
    record = {
        "reasoning_fingerprint": fingerprint,
        "model": model,
        "created_at": datetime.now(UTC).isoformat(),
        "dev_scenario_ids": [
            key for key, (split, _) in KNOWN.items() if split == "dev"
        ],
        "holdout_scenario_ids": [
            key for key, (split, _) in KNOWN.items() if split == "holdout"
        ],
        "dev_evaluation_id": dev_result["evaluation_id"],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, indent=2)
        stream.write("\n")
    return record


def require_freeze(path, model, fingerprint):
    if path is None or not path.is_file():
        raise ValueError("Live holdout requires an existing freeze")
    record = json.loads(path.read_text(encoding="utf-8"))
    if (
        record.get("reasoning_fingerprint") != fingerprint
        or record.get("model") != model
    ):
        raise ValueError("Reasoning fingerprint/model differs from holdout freeze")
    for split in ("dev", "holdout"):
        if record.get(f"{split}_scenario_ids") != [
            key for key, (value, _) in KNOWN.items() if value == split
        ]:
            raise ValueError("Freeze scenario split mismatch")
    return record
