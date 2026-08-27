#!/usr/bin/env python3
"""Snapshot the generated node tree, playlists and skin props of a live Kodi.

The phase-2 node oracle (docs/sync-refactor-phase2-plan.md §3): every file
kofin generates under the profile's ``library/video/kofin``,
``library/music/kofin``, ``playlists/video/Kofin`` and
``playlists/music/Kofin`` as ``{relative path: text}``, plus every
``Kofin.nodes.*`` / ``Kofin.wnodes.*`` window property read over JSON-RPC.
Two snapshots of the same state must be byte-identical; ``diff`` says
where they are not.

    node_snapshot.py tree OUT.json --kodi-home DIR [--profile NAME]
    node_snapshot.py props OUT.json [--port 8080] [--user kodi --password kodi]
    node_snapshot.py diff BEFORE.json AFTER.json
"""

import argparse
import json
import os
import sys
import urllib.request

TREES = (
    ("library/video", "kofin"),
    ("library/music", "kofin"),
    ("playlists/video", "Kofin"),
    ("playlists/music", "Kofin"),
)

# The props window_nodes writes per entry; the sub-node ones are the NODES
# keys (views.py). Read generously — a prop that is not set reads back "".
NODE_PROPS = ("index", "id", "path", "artwork", "title", "content", "type")
SUB_NODES = (
    "all",
    "recent",
    "recentepisodes",
    "inprogress",
    "inprogressepisodes",
    "nextepisodes",
    "genres",
    "random",
    "recommended",
    "unwatched",
    "sets",
)
SUB_PROPS = ("title", "content", "path", "id", "type", "artwork")


def profile_dir(kodi_home, profile):
    base = os.path.join(kodi_home, "userdata")
    return os.path.join(base, "profiles", profile) if profile else base


def snapshot_tree(kodi_home, profile):
    root = profile_dir(kodi_home, profile)
    files = {}
    for parent, name in TREES:
        top = os.path.join(root, parent, name)
        if not os.path.isdir(top):
            continue
        for dirpath, dirnames, filenames in os.walk(top):
            dirnames.sort()
            for filename in sorted(filenames):
                path = os.path.join(dirpath, filename)
                key = os.path.relpath(path, root)
                with open(path, "rb") as handle:
                    data = handle.read()
                try:
                    files[key] = data.decode("utf-8")
                except UnicodeDecodeError:
                    files[key] = "<binary %d bytes>" % len(data)
    return files


def rpc(port, user, password, method, params):
    body = json.dumps(
        {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    ).encode()
    request = urllib.request.Request(
        "http://localhost:%d/jsonrpc" % port,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    import base64

    token = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
    request.add_header("Authorization", "Basic " + token)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)["result"]


def labels(port, user, password, names):
    out = {}
    for start in range(0, len(names), 40):
        chunk = names[start : start + 40]
        result = rpc(
            port,
            user,
            password,
            "XBMC.GetInfoLabels",
            {"labels": ["Window(10000).Property(%s)" % name for name in chunk]},
        )
        for name in chunk:
            out[name] = result.get("Window(10000).Property(%s)" % name, "")
    return out


def snapshot_props(port, user, password):
    props = {}
    for prefix in ("Kofin.nodes", "Kofin.wnodes"):
        total = labels(port, user, password, ["%s.total" % prefix])["%s.total" % prefix]
        props["%s.total" % prefix] = total
        count = int(total or 0)
        names = []
        for index in range(count):
            for prop in NODE_PROPS:
                names.append("%s.%d.%s" % (prefix, index, prop))
            for sub in SUB_NODES:
                for prop in SUB_PROPS:
                    names.append("%s.%d.%s.%s" % (prefix, index, sub, prop))
        # One past the end too: a stale entry a rebuild failed to clear.
        for prop in NODE_PROPS:
            names.append("%s.%d.%s" % (prefix, count, prop))
        for name, value in labels(port, user, password, names).items():
            if value != "":
                props[name] = value
    return props


def diff(before, after):
    keys = sorted(set(before) | set(after))
    changed = 0
    for key in keys:
        if key not in before:
            print("+ %s" % key)
            changed += 1
        elif key not in after:
            print("- %s" % key)
            changed += 1
        elif before[key] != after[key]:
            print("~ %s" % key)
            print("    before: %s" % before[key][:200].replace("\n", "\\n"))
            print("    after:  %s" % after[key][:200].replace("\n", "\\n"))
            changed += 1
    print(
        "RESULT: %s (%d entries before, %d after, %d differ)"
        % (
            "identical" if not changed else "DIFFERENT",
            len(before),
            len(after),
            changed,
        )
    )
    return 0 if not changed else 1


def main(argv):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="mode", required=True)
    tree = sub.add_parser("tree")
    tree.add_argument("out")
    tree.add_argument("--kodi-home", required=True)
    tree.add_argument("--profile")
    props = sub.add_parser("props")
    props.add_argument("out")
    props.add_argument("--port", type=int, default=8080)
    props.add_argument("--user", default="kodi")
    props.add_argument("--password", default="kodi")
    compare = sub.add_parser("diff")
    compare.add_argument("before")
    compare.add_argument("after")
    args = parser.parse_args(argv)

    if args.mode == "tree":
        data = snapshot_tree(args.kodi_home, args.profile)
    elif args.mode == "props":
        data = snapshot_props(args.port, args.user, args.password)
    else:
        with open(args.before) as handle:
            before = json.load(handle)
        with open(args.after) as handle:
            after = json.load(handle)
        return diff(before, after)

    with open(args.out, "w") as handle:
        json.dump(data, handle, indent=1, sort_keys=True)
    print("%s: %d entries -> %s" % (args.mode, len(data), args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
