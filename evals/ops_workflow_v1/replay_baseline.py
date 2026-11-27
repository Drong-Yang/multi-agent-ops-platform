"""Rerun the existing SQLite regression and Wiki drill into a new evidence directory."""
import argparse
import os
from pathlib import Path
import runpy
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SQLITE_SUITES = (
    "tests.test_mcp_tools", "tests.test_index_jobs", "tests.test_support_wiki",
    "tests.test_fusion_workflow", "tests.test_repository",
)


def replay_wiki(output):
    replay = runpy.run_path(str(ROOT / "scripts/replay_wiki_drill.py"))["main"]
    replay.__globals__.update(RUNS=output, write_documents=lambda documents: None)
    replay()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "MOCK_MODE": "true", "FUSION_KB_MODE": "sample", "PYTHONIOENCODING": "utf-8"}
    env.pop("FUSION_TEST_DATABASE_URL", None)
    with (output / "sqlite-unittest.log").open("wb") as log:
        result = subprocess.run(
            [sys.executable, "-m", "unittest", *SQLITE_SUITES, "-v"],
            cwd=ROOT / "backend", env=env, stdout=log, stderr=subprocess.STDOUT,
        )
    replay_wiki(output)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
