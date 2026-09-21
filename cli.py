#!/usr/bin/env python3
"""Read a route() payload from stdin and print the decision JSON."""

from __future__ import annotations

import json
import sys
from typing import Any

from router import RouterConfigError, RouterResult, result_json, route


def main() -> int:
    """CLI entry: stdin JSON → stdout RouterResult JSON."""
    raw: str = sys.stdin.read()
    if not raw.strip():
        print("cli.py: expected a JSON payload on stdin", file=sys.stderr)
        return 2
    try:
        payload: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"cli.py: invalid JSON on stdin: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("cli.py: payload must be a JSON object", file=sys.stderr)
        return 2
    try:
        result: RouterResult = route(payload)
    except RouterConfigError as exc:
        print(f"cli.py: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(result_json(result) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
