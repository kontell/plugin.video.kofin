#!/usr/bin/env python3
"""What Jellyfin answers a user who can see no library (audit F2's trigger).

Creates a throwaway user on jf12 with ``EnableAllFolders=False``, no
``EnabledFolders`` and no Live TV, queries ``/UserViews``, ``/Library/
MediaFolders`` and a recursive ``/Items`` as that user, prints the shapes,
and deletes the user again. Nothing else on the server is touched.

Verified answer on jf12 v12 (2026-08-29)::

    /UserViews             -> 200 {'Items': 0, 'TotalRecordCount': 0}
    /Library/MediaFolders  -> 403
    /Items (recursive)     -> 200 {'Items': 0, 'TotalRecordCount': 0}

which is exactly the shape ``sync/views.py`` treated as a complete listing
before H3. With Live TV still granted the user gets one view (the Live TV
one) — the reason H3's floor gates on the whitelist, not on "no views".

    tests/live/jf12_user_policy.py [--keep-livetv]
"""

import argparse
import uuid

from jf12api import Jf12, shape


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--keep-livetv",
        action="store_true",
        help="leave EnableLiveTvAccess on (shows the one-view case)",
    )
    args = parser.parse_args()

    jf = Jf12()
    jf.login_admin()
    print("target:", jf.base)

    name = "probe-nofolders-" + uuid.uuid4().hex[:6]
    password = uuid.uuid4().hex
    user = jf.create_user(name, password)
    user_id = user["Id"]
    print("throwaway user created:", name)
    try:
        policy = dict(user["Policy"])
        policy.update(
            EnableAllFolders=False, EnabledFolders=[], EnableMediaPlayback=True
        )
        if not args.keep_livetv:
            policy.update(EnableLiveTvAccess=False, EnableLiveTvManagement=False)
        print("policy ->", jf.set_policy(user_id, policy))

        token = jf.login(name, password)
        for path in (
            "/UserViews",
            "/UserViews?userId=" + user_id,
            "/Library/MediaFolders",
            "/Items?Recursive=true&IncludeItemTypes=Movie&Limit=1&EnableTotalRecordCount=true",
        ):
            status, content_type, body = jf.call("GET", path, None, token)
            label = path.split("?")[0] + ("?" if "?" in path else "")
            print(
                "%-28s -> %s %s %s"
                % (label, status, content_type.split(";")[0], shape(body))
            )
            if isinstance(body, dict) and body.get("Items"):
                print(
                    "    views:",
                    [
                        (item.get("Name"), item.get("CollectionType"))
                        for item in body["Items"]
                    ],
                )
        status, _, body = jf.call("GET", "/UserViews", None, jf.admin_token)
        print("%-28s -> %s %s" % ("control: admin /UserViews", status, shape(body)))
    finally:
        print("throwaway user deleted ->", jf.delete_user(user_id))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
