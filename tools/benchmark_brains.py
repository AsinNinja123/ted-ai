#!/usr/bin/env python3
"""Compare Ted's cloud and local brains on safe tool-routing cases.

No real tool handler is imported or executed. Both providers receive the same
persona, dynamically selected schemas, compact context, and fake tool results.

Examples:
    venv/bin/python tools/benchmark_brains.py --provider cloud
    venv/bin/python tools/benchmark_brains.py --provider local
    venv/bin/python tools/benchmark_brains.py --provider both --pause 12
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import llm, providers, routing, tool_handlers


CASES = (
    {
        "name": "mixed app and website",
        "prompt": "Open Notes and open youtube.com in Brave.",
        "expected": ["open_app", "browse_to"],
    },
    {
        "name": "dependent clipboard chain",
        "prompt": (
            "Put the exact text TED TOOL TEST on my clipboard, then tell me what "
            "is on my clipboard."
        ),
        "expected": ["clipboard_write", "clipboard_read"],
    },
    {
        "name": "contextual app request",
        "prompt": "Close the two apps I just opened.",
        "expected": ["close_app", "close_app"],
        "operational": (
            "Recent verified actions: open_app({'name': 'Notes'}) -> Opened Notes. | "
            "open_app({'name': 'Calendar'}) -> Opened Calendar."
        ),
    },
)


RESULTS = {
    "open_app": "Opened Notes.",
    "close_app": "Closed app.",
    "browse_to": "Opened youtube.com in Brave Browser.",
    "clipboard_write": "Copied it.",
    "clipboard_read": "TED TOOL TEST",
}


def _disable_memory():
    llm.detect_action = lambda _text: None
    llm.get_memory = lambda _query: ""
    llm.get_facts_about = lambda _subject: ""
    llm.format_memories_for_prompt = lambda: ""
    llm.save_memory = lambda *_args, **_kwargs: None
    llm.extract_and_save_facts = lambda *_args, **_kwargs: None
    llm.intents._worth_extracting = lambda _text: False
    llm.features.HAS_KNOWLEDGE = False


def run_case(case):
    calls = []
    schemas = routing.select_tool_schemas(
        case["prompt"], case.get("operational", ""))
    runtime = None

    def dispatch(name, args):
        if name == "find_tools":
            added = runtime.add_schemas(routing.discover_tool_schemas(
                args.get("query", ""), exclude=runtime.schema_by_name))
            return "Loaded capabilities: " + ", ".join(added)
        calls.append((name, args))
        return RESULTS.get(name, f"{name} completed.")

    runtime = llm.ToolRuntime(
        schemas=schemas,
        dispatch=dispatch,
        action_tools=tool_handlers.ACTION_TOOLS,
        is_failure=tool_handlers.looks_like_failure,
    )
    conversation = [{"role": "system", "content": llm.SYSTEM_PROMPT}]
    started = time.perf_counter()
    answer = "".join(llm.ask_streaming(
        case["prompt"], conversation,
        tool_runtime=runtime,
        context_scope="none",
        operational_context=case.get("operational", ""),
        require_tool=True,
        min_action_calls=routing.expected_action_calls(case["prompt"]),
    ))
    elapsed = time.perf_counter() - started
    actual = [name for name, _args in calls]
    return {
        "case": case["name"],
        "seconds": round(elapsed, 3),
        "expected": case["expected"],
        "actual": actual,
        "pass": actual == case["expected"],
        "answer": answer.strip(),
    }


def run_provider(label, pause):
    original_groq = providers._groq
    original_local = providers._ollama_create
    if label == "local":
        providers._groq = None
    else:
        # A cloud benchmark must not silently score a local fallback as cloud.
        providers._ollama_create = lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("cloud benchmark fallback disabled"))
    rows = []
    try:
        for index, case in enumerate(CASES):
            if index and pause:
                time.sleep(pause)
            try:
                row = run_case(case)
            except Exception as exc:
                row = {
                    "case": case["name"], "seconds": None,
                    "expected": case["expected"], "actual": [], "pass": False,
                    "answer": f"ERROR: {exc}",
                }
            row["provider"] = label
            rows.append(row)
            mark = "PASS" if row["pass"] else "FAIL"
            print(f"{label:5} {mark:4} {row['case']}: {row['seconds']}s "
                  f"calls={row['actual']}")
    finally:
        providers._groq = original_groq
        providers._ollama_create = original_local
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("cloud", "local", "both"),
                        default="both")
    parser.add_argument("--pause", type=float, default=0.0,
                        help="seconds between cases (useful on a free cloud tier)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    _disable_memory()
    labels = ("cloud", "local") if args.provider == "both" else (args.provider,)
    rows = []
    for label in labels:
        rows.extend(run_provider(label, args.pause if label == "cloud" else 0.0))
    if args.json:
        print(json.dumps(rows, indent=2))
    passed = sum(row["pass"] for row in rows)
    print(f"\n{passed}/{len(rows)} cases passed")
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
