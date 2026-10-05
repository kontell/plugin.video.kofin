#!/usr/bin/env python3
"""Check a phase-0 public API capture against the OR implementation floor.

This is a research/CI contract, not runtime backend selection. Passing proves
interface presence only: the parity ledger holds behavioral release gates.
"""

import argparse
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "docs/research/kofin-or/phase0/requirements.json"


def version(value):
    return tuple(int(part) for part in value.split("."))


def check(capture, contract, allow_dirty=False):
    capture = capture.get("result", capture)
    errors = []
    application = capture.get("application", {}).get("version", {})
    if application.get("major", 0) < contract["minimum_kodi_major"]:
        errors.append("Kodi 22 Piers or later is required")
    for addon, minimum in contract["system_addons"].items():
        try:
            adequate = version(
                capture.get("system_addons", {}).get(addon, "0")
            ) >= version(minimum)
        except ValueError:
            adequate = False
        if not adequate:
            errors.append(addon + " >= " + minimum + " is required")
    dirty = "dirty" in application.get("revision", "").lower()
    if dirty and not allow_dirty:
        errors.append(
            "dirty build requires explicit phase-0 exception; not stock qualification"
        )
    for method, parameters in contract["methods"].items():
        definition = capture.get("methods", {}).get(method)
        if definition is None:
            errors.append("missing method " + method)
            continue
        actual = {param["name"]: param for param in definition.get("params", [])}
        for name, required in parameters.items():
            if name not in actual:
                errors.append("missing parameter " + method + "." + name)
            elif required and not set(required).issubset(actual[name].get("enums", [])):
                errors.append("missing enum values " + method + "." + name)
    for field, methods in contract["python"].items():
        for method in methods:
            if capture.get(field, {}).get(method) is not True:
                errors.append("missing Python capability " + field + "." + method)
    return {
        "interfaces_passed": not errors,
        "errors": errors,
        "dirty_build_exception_used": dirty and allow_dirty,
        "stock_release_qualified": False,
        "behavioral_gates": contract["behavioral_gates"],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("capture", type=Path)
    parser.add_argument("--contract", type=Path, default=CONTRACT)
    parser.add_argument(
        "--allow-dirty", action="store_true", help="research exception only"
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
