#!/usr/bin/env python3
"""Replay an omitted-content incident only in the source checkout's fake harness.

python3 scripts/replay_incident.py fixture.json --inject drop_ack@0
Faults: drop@EVENT, crash@EVENT, drop_ack@EVENT. Delays use explicit gates in tests.
No operator configuration, model, shell commands, credentials or uploads.
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from codex_local_mcp.incidents import MAX_BYTES, validate_incident
from codex_local_mcp.security import BridgeError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("fixture", type=Path)
    parser.add_argument("--inject", action="append", default=[], help="drop@EVENT, crash@EVENT or drop_ack@EVENT")
    args = parser.parse_args()
    try:
        if args.fixture.stat().st_size > MAX_BYTES:
            raise BridgeError("Incident exceeds the 2 MiB export budget", code="result_too_large")
        fixture = validate_incident(json.loads(args.fixture.read_text(encoding="utf-8")))
        faults = []
        for item in args.inject:
            action, position = item.split("@")
            if action not in ("drop", "crash", "drop_ack"):
                raise ValueError("Unsupported fault")
            faults.append({"action": action, "at": int(position)})
        from tests.incident_replay import IncidentReplay
        print(json.dumps(asyncio.run(IncidentReplay(fixture, injections=faults).run()), indent=2))
        return 0
    except (OSError, ValueError, BridgeError, ImportError, KeyError, TypeError, RecursionError) as exc:
        print(str(exc) if isinstance(exc, BridgeError) else "Cannot replay incident; use a valid structural fixture and a source checkout", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
