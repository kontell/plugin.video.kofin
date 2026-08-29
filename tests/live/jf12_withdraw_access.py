#!/usr/bin/env python3
"""Withdraw, and put back, a real jf12 user's library access (fixes plan S-H3).

``withdraw NAME`` saves the user's current policy beside this script's
results directory and sets EnableAllFolders=False, EnabledFolders=[] and no
Live TV — the shape that answers /UserViews with zero items. ``restore NAME``
writes the saved policy back and deletes the save. Nothing else on the
server is touched; the media rule (no item deletes, no library writes) holds.

    tests/live/jf12_withdraw_access.py withdraw kofin-test
    tests/live/jf12_withdraw_access.py restore kofin-test
"""

import argparse
import json
import os

from jf12api import Jf12, shape

SAVE_DIR = os.path.join(os.path.dirname(__file__), "results", "S-H3")


def _user(jf, name):
    status, _, users = jf.call("GET", "/Users", None, jf.admin_token)
    if status != 200:
        raise SystemExit("user listing failed: %s" % status)
    for user in users:
        if user.get("Name") == name:
            return user
    raise SystemExit("no user named %s" % name)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("withdraw", "restore", "show"))
    parser.add_argument("name")
    args = parser.parse_args()

    jf = Jf12()
    jf.login_admin()
    user = _user(jf, args.name)
    save_path = os.path.join(SAVE_DIR, "%s-policy.json" % args.name)

    if args.action == "show":
        policy = user["Policy"]
        print(
            "%s: EnableAllFolders=%s EnabledFolders=%d EnableLiveTvAccess=%s"
            % (
                args.name,
                policy.get("EnableAllFolders"),
                len(policy.get("EnabledFolders") or []),
                policy.get("EnableLiveTvAccess"),
            )
        )
        return 0

    if args.action == "withdraw":
        if os.path.exists(save_path):
            raise SystemExit(
                "a saved policy already exists at %s; restore first" % save_path
            )
        os.makedirs(SAVE_DIR, exist_ok=True)
        with open(save_path, "w") as handle:
            json.dump(user["Policy"], handle, indent=1, sort_keys=True)
        policy = dict(user["Policy"])
        policy.update(
            EnableAllFolders=False,
            EnabledFolders=[],
            EnableLiveTvAccess=False,
            EnableLiveTvManagement=False,
        )
        print("policy withdrawn ->", jf.set_policy(user["Id"], policy))
        print("saved original to", save_path)
        return 0

    with open(save_path) as handle:
        saved = json.load(handle)
    print("policy restored ->", jf.set_policy(user["Id"], saved))
    os.remove(save_path)
    status, _, body = jf.call("GET", "/Users/%s" % user["Id"], None, jf.admin_token)
    print("now:", shape({k: v for k, v in body["Policy"].items() if "Folder" in k}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
