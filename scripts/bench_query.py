#!/usr/bin/env python3
"""End-to-end timing benchmark for the query procedure (`scripts/query.py`).

Reads the golden questions from `tests/fixtures/golden_query_cases.md` (so the
benchmark stays in sync with the test set) and runs one question per iteration
through `query.run_query`. Questions are selected in fixture order and cycle
when the requested run count exceeds the number of questions.

This measures the *real* end-to-end path — including the model call — so it
must run in an environment that can reach the model (i.e. with OPENAI_API_KEY
set and outbound access). It will not produce timings in a sandbox that blocks
the API; use it in your own environment.

Cache is OFF by default (clean measurement: every question does full
resolution and nothing is written back). Turn either half on with
--cache-read / --cache-write if you want to measure the cached path instead.

Usage:
    python3 scripts/bench_query.py                     # 5 questions, cache off
    python3 scripts/bench_query.py --runs 3
    python3 scripts/bench_query.py --cache-read        # measure the cache-hit path
    python3 scripts/bench_query.py --model gpt-5.6
    python3 scripts/bench_query.py --list              # just show the questions, no run
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import query  # noqa: E402  (scripts/query.py)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = ROOT / "tests" / "fixtures" / "golden_query_cases.md"
_Q_RE = re.compile(r"^\s*-\s*\*\*Question \(verbatim\):\*\*\s*(.+?)\s*$")


def load_questions(path: Path = GOLDEN) -> list[str]:
    """Extract the verbatim questions from the golden fixture, in file order."""
    if not path.exists():
        raise SystemExit(f"golden fixture not found: {path}")
    questions: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _Q_RE.match(line)
        if m:
            questions.append(m.group(1))
    if not questions:
        raise SystemExit(f"no questions parsed from {path}")
    return questions


def _extract_name(question: str) -> str:
    """Light heuristic to pick an entity name to resolve, for --local timing."""
    caps = re.findall(r"\b[A-Z]{2,}\b", question)
    if caps:
        return caps[0]
    stop = {"What", "Who", "Tell", "How", "Give", "So", "The", "Is", "Are", "Where", "When", "Me"}
    for seq in re.findall(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", question):
        if seq.split()[0] not in stop:
            return seq
    return question


def _time_local_case(question: str) -> str:
    """Deterministic-only work: resolve an entity + read its note (no model)."""
    r = query.tool_resolve_entity(_extract_name(question))
    if not r["matches"]:
        return "no match"
    m = r["matches"][0]
    query.tool_read_note(m["domain"], m.get("file") or m["id"])
    return "ok (local)"


def run_benchmark(questions: list[str], runs: int, cache_read: bool,
                  cache_write: bool, model: str | None, local: bool = False) -> None:
    width = min(max((len(q) for q in questions), default=20), 52)
    per_case_ms: list[list[float]] = [[] for _ in questions]
    run_times: list[float] = []

    if local:
        print(f"Golden query benchmark — {len(questions)} cases, {runs} iteration(s), "
              f"mode=local (deterministic resolve+read only, NO model)\n")
    else:
        print(f"Golden query benchmark — {len(questions)} cases, {runs} iteration(s), "
              f"cache_read={'on' if cache_read else 'off'} "
              f"cache_write={'on' if cache_write else 'off'}, "
              f"model={model or query.DEFAULT_MODEL}\n")

    for r in range(runs):
        i = r % len(questions)
        q = questions[i]
        start = time.perf_counter()
        status = "ok"
        try:
            if local:
                status = _time_local_case(q)
            else:
                query.run_query(q, cache_read=cache_read,
                                cache_write=cache_write, model=model)
        except query.QueryError as exc:
            status = f"ERROR: {exc}"[:60]
        ms = (time.perf_counter() - start) * 1000
        per_case_ms[i].append(ms)
        run_times.append(ms)
        print(f"Run {r + 1} (case {i + 1}): "
              f"{q[:width]:<{width}} {ms:9.1f} ms  {status}")

    print("\nMean per exercised case:")
    for i, q in enumerate(questions):
        if not per_case_ms[i]:
            continue
        mean = sum(per_case_ms[i]) / len(per_case_ms[i])
        print(f"  {i + 1}. {q[:width]:<{width}} {mean:9.1f} ms")

    overall = sum(run_times) / len(run_times)
    print(f"\nAverage per iteration/query: {overall:9.1f} ms")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Time the golden query cases end-to-end.")
    parser.add_argument("--runs", type=int, default=5,
                        help="number of one-question iterations (default 5)")
    parser.add_argument("--cache-read", dest="cache_read", action="store_true", default=False,
                        help="allow the Step 0 cache short-circuit (default off)")
    parser.add_argument("--cache-write", dest="cache_write", action="store_true", default=False,
                        help="persist answers to entities/search/ (default off)")
    parser.add_argument("--model", default=None, help=f"model override (default {query.DEFAULT_MODEL})")
    parser.add_argument("--local", action="store_true",
                        help="time only the deterministic resolve+read layer (no model; runs anywhere)")
    parser.add_argument("--list", action="store_true", help="just print the parsed questions and exit")
    args = parser.parse_args(argv)

    questions = load_questions()
    if args.list:
        for i, q in enumerate(questions, 1):
            print(f"{i}. {q}")
        return 0

    run_benchmark(questions, args.runs, args.cache_read, args.cache_write,
                  args.model, local=args.local)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
