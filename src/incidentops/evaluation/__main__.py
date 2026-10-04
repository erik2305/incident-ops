"""One stdlib CLI, using the existing root environment names."""

import argparse
import asyncio
import os
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from incidentops.evaluation.freeze import (
    reasoning_fingerprint,
    require_freeze,
    write_freeze,
)
from incidentops.evaluation.report import candidate_summary, save_report
from incidentops.evaluation.runner import EvaluationConfig, run_scenario
from incidentops.evaluation.scenarios import Environment, load_scenarios


def configuration(provider):
    names = {
        "database": "INCIDENTOPS_TEST_DATABASE_URL",
        "checkout": "INCIDENTOPS_TEST_CHECKOUT_URL",
        "inventory": "INCIDENTOPS_TEST_INVENTORY_URL",
        "observability": "INCIDENTOPS_TEST_OBSERVABILITY_MCP_URL",
        "operations": "INCIDENTOPS_TEST_OPERATIONS_MCP_URL",
    }
    values = {key: os.environ.get(name, "") for key, name in names.items()}
    if any(not value.strip() for value in values.values()):
        raise ValueError("All five existing integration URL variables are required")
    key = os.environ.get("OPENROUTER_API_KEY", "") if provider == "live" else ""
    if provider == "live" and not key.strip():
        raise ValueError("Live evaluation requires OPENROUTER_API_KEY")
    return EvaluationConfig(
        **values,
        model=os.environ.get("INCIDENTOPS_TEST_LLM_MODEL", "openai/gpt-6-luna"),
        api_key=key,
    )


async def evaluate(args, config):
    scenarios = load_scenarios(Path("scenarios"))
    selected = [s for s in scenarios if args.split == "all" or s.split == args.split]
    fingerprint = reasoning_fingerprint(config.model)
    freeze = None
    if args.provider == "live" and args.split == "holdout":
        freeze = require_freeze(args.require_freeze, config.model, fingerprint)
    if args.write_freeze and args.write_freeze.exists():
        raise ValueError(
            "Freeze already exists; choose a new evaluation invocation path"
        )
    name = (
        "scripted-v1-candidate"
        if args.provider == "scripted"
        else f"live-{args.split}-v1-candidate"
    )
    path = args.output / f"{name}.json"
    if path.exists() or path.with_suffix(".md").exists():
        raise ValueError(
            "Results already exist; choose another output directory to preserve history"
        )
    args.output.mkdir(parents=True, exist_ok=True)
    data = {
        "evaluation_id": uuid4().hex,
        "created_at": datetime.now(UTC).isoformat(),
        "provider": args.provider,
        "split": args.split,
        "runs_per_scenario": args.runs,
        "model": config.model if args.provider == "live" else "scripted",
        "reasoning_fingerprint": fingerprint,
        "freeze": freeze,
        "scenarios": {s.id: s.model_dump(mode="json") for s in selected},
        "complete": False,
    }
    runs = []
    environment = Environment(config.checkout, config.inventory)
    try:
        save_report(path, data, runs)
        for scenario in selected:
            for attempt in range(1, args.runs + 1):
                result = await run_scenario(
                    scenario, attempt, args.provider, config, environment
                )
                runs.append(result)
                save_report(path, data, runs)
                print(
                    f"{scenario.id} {attempt}/{args.runs}: {result.outcome}; cause={result.observed_root_cause}; status={result.observed_terminal_status}; mutations={result.observed_mutation_count}",
                    flush=True,
                )
    finally:
        await environment.close()
    data["complete"] = True
    save_report(path, data, runs)
    if args.provider == "scripted" and any(r.outcome != "pass" for r in runs):
        return 1
    if args.write_freeze:
        write_freeze(args.write_freeze, config.model, fingerprint, data)
    if args.provider == "live" and args.split == "holdout":
        candidate_summary(args.output)
    print(f"Recorded {len(runs)} attempts in {path}; {data['aggregate']}", flush=True)
    return 2 if any(r.security_blocker for r in runs) else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("scripted", "live"), required=True)
    parser.add_argument("--split", choices=("all", "dev", "holdout"), required=True)
    parser.add_argument("--runs", type=int, required=True)
    parser.add_argument("--output", type=Path, default=Path("results/evaluation"))
    parser.add_argument("--write-freeze", type=Path)
    parser.add_argument("--require-freeze", type=Path)
    args = parser.parse_args()
    if args.provider == "scripted" and (
        args.runs != 1
        or args.split != "all"
        or args.write_freeze
        or args.require_freeze
    ):
        parser.error(
            "Scripted calibration runs all six scenarios once, without a freeze"
        )
    if args.provider == "live" and (args.runs != 3 or args.split == "all"):
        parser.error("Live evaluation runs dev or holdout exactly three times each")
    if args.write_freeze and (args.provider != "live" or args.split != "dev"):
        parser.error("Write freeze only after live dev")
    if args.require_freeze and (args.provider != "live" or args.split != "holdout"):
        parser.error("Require freeze only for live holdout")
    load_dotenv(Path.cwd() / ".env")
    try:
        with asyncio.Runner(loop_factory=asyncio.SelectorEventLoop) as runner:
            return runner.run(evaluate(args, configuration(args.provider)))
    except ValueError:
        # Do not print exception messages which may originate from external schemas.
        parser.exit(
            1,
            "Evaluation configuration/freeze/output validation failed; no unsafe run was started.\n",
        )


if __name__ == "__main__":
    raise SystemExit(main())
