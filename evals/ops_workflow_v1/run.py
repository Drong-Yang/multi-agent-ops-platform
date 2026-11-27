"""Replay independent collaboration scenarios and persist each attempt immediately."""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ.update(MOCK_MODE="true", KB_MODE="sample", FUSION_KB_MODE="sample", DEEPSEEK_API_KEY="")

from harness import fixture, postgres_admin
from operations import creation, routing, wiki
from permissions import HANDLERS as MCP
from recovery import HANDLERS as RECOVERY
from scenarios import BUILDERS, REPRESENTATIVES

FAMILIES = {"routing": 50, "wiki": 60, "creation": 40, "mcp": 50, "recovery": 40}
SEED = "ops_workflow_v1-fixed-fixtures"


def deny_external_network(event, args):
    if event == "socket.connect":
        address = args[1]
        if isinstance(address, tuple) and address[0] not in {"127.0.0.1", "::1"}:
            raise RuntimeError("External network disabled for collaboration replay")
    if event == "socket.getaddrinfo" and args[0] not in {"localhost", "127.0.0.1", "::1", None}:
        raise RuntimeError("External DNS disabled for collaboration replay")


def dump(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


async def execute(s, case):
    family = case["family"]
    if family == "mcp":
        return await MCP[case["action"]](s, case)
    if family == "recovery":
        return await RECOVERY[case["action"]](s, case["inputs"]["variant"])
    return await {"routing": routing, "wiki": wiki, "creation": creation}[family](s, case)


async def run_attempt(admin, case, attempt):
    trace = {"case_id": case["case_id"], "family": case["family"], "split": case["split"], "attempt": attempt}
    begin = time.monotonic()
    try:
        async with fixture(admin, trace) as scenario:
            state = await asyncio.wait_for(execute(scenario, case), 25)
            trace["actual_state"] = state
            assert state == case["expected_state"], (state, case["expected_state"])
        trace["status"] = "passed"
    except Exception as exc:
        trace.update(status="failed", error=type(exc).__name__, detail=str(exc),
                     traceback=traceback.format_exc())
    trace["elapsed_ms"] = round((time.monotonic() - begin) * 1000)
    return trace


def summarize(cases, results):
    by_id = {case["case_id"]: [] for case in cases}
    for row in results:
        by_id[row["case_id"]].append(row["status"])
    counts = {}
    for family in FAMILIES:
        group = [case for case in cases if case["family"] == family]
        states = {"passed": 0, "failed": 0, "not_run": 0, "repeats": 0, "repeat_failures": 0}
        for case in group:
            rows = by_id[case["case_id"]]
            status = "failed" if "failed" in rows else "passed" if rows else "not_run"
            states[status] += 1
            states["repeats"] += max(0, len(rows) - 1)
            states["repeat_failures"] += rows[1:].count("failed")
        counts[family] = states
    return counts


async def run(args, cases, selected, out):
    admin = postgres_admin() if args.engine == "postgres" else None
    version = sqlite3.sqlite_version
    try:
        if admin is not None:
            with admin.connect() as connection:
                version = connection.exec_driver_sql("SHOW server_version").scalar_one()
        dump(out / "environment.json", {
            "at": datetime.now(timezone.utc).isoformat(), "python": platform.python_version(),
            "platform": platform.platform(), "engine": args.engine, "database_version": version,
            "database": "per-case file" if admin is None else "commerce_fusion_test / per-case schema",
            "head": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
            "seed": SEED, "corpus": "knowledge/snapshots/ecommerce-v2.json",
            "corpus_file_sha256": hashlib.sha256((ROOT / "knowledge/snapshots/ecommerce-v2.json").read_bytes()).hexdigest(),
            "backend_sha256": {
                file: hashlib.sha256((ROOT / file).read_bytes()).hexdigest()
                for file in ("backend/app/fusion/api.py", "backend/app/fusion/runtime.py",
                             "backend/app/fusion/repository.py", "backend/app/fusion/wiki_drafts.py")
            },
            "retrieval": "sample lexical", "providers": "mock, external network guarded",
            "selector": args.select, "additional_attempts": args.repeats,
        })
        results = []
        with (out / "results.jsonl").open("w", encoding="utf-8", buffering=1) as stream:
            for case in selected:
                attempts = 1 + (args.repeats if case["repeat"] else 0)
                for attempt in range(1, attempts + 1):
                    result = await run_attempt(admin, case, attempt)
                    results.append(result)
                    stream.write(json.dumps(result, ensure_ascii=False) + "\n")
                    if attempt == 1 or result["status"] == "failed":
                        print(f"{case['case_id']} #{attempt} {result['status']} {result.get('detail', '')}", flush=True)
            selected_ids = {case["case_id"] for case in selected}
            for case in cases:
                if case["case_id"] not in selected_ids:
                    stream.write(json.dumps({
                        "case_id": case["case_id"], "family": case["family"],
                        "split": case["split"], "status": "not_run",
                    }, ensure_ascii=False) + "\n")
        counts = summarize(cases, results)
        dump(out / "summary.json", {
            "families": counts,
            "attempts": len(results), "attempt_counts": dict(Counter(row["status"] for row in results)),
            "unique_executed": len(selected),
            "split_counts": dict(Counter(case["split"] for case in selected)),
            "failed_ids": sorted({row["case_id"] for row in results if row["status"] == "failed"}),
        })
        return int(any(row["status"] == "failed" for row in results))
    finally:
        if admin is not None:
            admin.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--select", choices=("representative", "all", "critical", "recovery", "development", "holdout"),
                        default="all")
    parser.add_argument("--engine", choices=("sqlite", "postgres"), default="sqlite")
    parser.add_argument("--case-id", action="append", default=[], help="Restrict replay to a case ID; repeat to select more")
    parser.add_argument("--repeats", type=int, default=0, help="Extra runs per critical case")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if args.repeats < 0:
        parser.error("--repeats must be non-negative")
    all_cases = [case for builder in BUILDERS for case in builder()]
    assert Counter(case["family"] for case in all_cases) == FAMILIES
    known_ids = {case["case_id"] for case in all_cases}
    assert len(known_ids) == len(all_cases)
    filter_ids = set(args.case_id)
    if filter_ids - known_ids:
        parser.error(f"Unknown case IDs: {', '.join(sorted(filter_ids - known_ids))}")
    selected = {
        "representative": lambda c: c["case_id"] in REPRESENTATIVES,
        "all": lambda c: True,
        "critical": lambda c: c["repeat"],
        "recovery": lambda c: c["family"] == "recovery",
        "development": lambda c: c["split"] == "development",
        "holdout": lambda c: c["split"] == "holdout",
    }[args.select]
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    dump(out / "scenarios.json", all_cases)
    dump(out / "invocation.json", {"command_arguments": sys.argv[1:], "workdir": str(ROOT)})
    sys.addaudithook(deny_external_network)
    cases = [case for case in all_cases if selected(case) and (not filter_ids or case["case_id"] in filter_ids)]
    return asyncio.run(run(args, all_cases, cases, out))


if __name__ == "__main__":
    raise SystemExit(main())
