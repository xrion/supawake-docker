#!/usr/bin/env python3
"""Automation entrypoint: one polling cycle, then report the run to the service."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gateway  # noqa: E402


def fire_callback(status: str = "COMPLETED", error: str | None = None) -> None:
    url = os.environ.get("AUTOMATION_CALLBACK_URL", "")
    if not url:
        return
    body = {"status": status, "run_id": os.environ.get("AUTOMATION_RUN_ID", "")}
    if error:
        body["error"] = error
    try:
        gateway.urlopen(
            gateway.Request(
                url,
                data=json.dumps(body).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {os.environ.get('AUTOMATION_CALLBACK_API_KEY', '')}",
                },
            ),
            timeout=15,
        )
    except Exception as exc:  # a lost callback must not fail the work already done
        print(f"Callback error: {exc}", file=sys.stderr)


def main() -> int:
    config = gateway.load_config()
    results = gateway.poll(config)
    notifications = gateway.notify_finished(config)
    print(json.dumps({"poll": results, "notify": notifications}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        fire_callback("FAILED", str(exc))
        sys.exit(1)
    fire_callback("COMPLETED")
