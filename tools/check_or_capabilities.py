#!/usr/bin/env python3
"""Check a public API capture against the shipped capability contract.

The capture is what ``capabilities.inspect`` reads inside Kodi, saved by
``tests/live/or_phase0/probe.py``: ``JSONRPC.Introspect``'s methods and
types, the application version, the system add-ons' versions and the Python
methods found on the info tags. The report says whether the baseline passes
and which feature each probe turned on. Passing proves interface presence
only: behavioural qualification is separate.
"""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))

from kofin.sync.backends.api.contract import check  # noqa: E402

CONTRACT = ROOT / "lib/kofin/sync/backends/api/requirements.json"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--contract", type=Path, default=CONTRACT)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="deprecated compatibility flag; build provenance is diagnostic only",
    )
    args = parser.parse_args()
    result = check(
        json.loads(args.capture.read_text()),
        json.loads(args.contract.read_text()),
        args.allow_dirty,
    )
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["interfaces_passed"] else 1)


if __name__ == "__main__":
    main()
