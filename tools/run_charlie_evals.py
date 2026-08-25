#!/usr/bin/env python3
"""Run the stable interpretation baseline without touching Charlie's Mac."""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core import understanding  # noqa: E402


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true", help="machine-readable results")
    args = parser.parse_args()
    path = os.path.join(ROOT, "evals", "charlie_scenarios.json")
    with open(path, encoding="utf-8") as fh:
        cases = json.load(fh)
    results = []
    for case in cases:
        active = ({"id": 1, "goal": case["active_goal"], "status": "active"}
                  if case.get("active_goal") else None)
        # The eval specifies action-likelihood for explicit action categories;
        # production obtains the same signal from routing.likely_action_request.
        action_likely = case["category"] in {
            "action", "multi_step", "correction", "format", "risk"
        } or case["prompt"].lower().startswith("forget ")
        got = understanding.resolve(
            case["prompt"], action_likely=action_likely, active_task=active)
        failures = []
        if got.mode != case["mode"]:
            failures.append(f"mode {got.mode!r} != {case['mode']!r}")
        if got.clarification_policy != case["clarification"]:
            failures.append(
                f"clarification {got.clarification_policy!r} != {case['clarification']!r}")
        results.append({"id": case["id"], "ok": not failures, "failures": failures,
                        "interpretation": got.as_dict()})
    passed = sum(item["ok"] for item in results)
    if args.json:
        print(json.dumps({"passed": passed, "total": len(results), "results": results}, indent=2))
    else:
        for item in results:
            if not item["ok"]:
                print(f"FAIL {item['id']}: {'; '.join(item['failures'])}")
        print(f"{passed}/{len(results)} Charlie scenarios passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
