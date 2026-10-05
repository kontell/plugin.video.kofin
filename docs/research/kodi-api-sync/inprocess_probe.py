import json
import pathlib
import time

import xbmc
import xbmcvfs

root = pathlib.Path(
    xbmcvfs.translatePath(
        "special://profile/addon_data/plugin.video.kofin.apiresearch/"
    )
)
records = []


def rpc(method, params=None):
    payload = (
        method
        if isinstance(method, list)
        else {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    )
    return json.loads(xbmc.executeJSONRPC(json.dumps(payload)))


movies = rpc(
    "VideoLibrary.GetMovies",
    {
        "filter": {"field": "tag", "operator": "is", "value": "kofin-api-research"},
        "properties": ["file"],
    },
)["result"]["movies"]
ids = [
    item["movieid"]
    for item in movies
    if item["file"].startswith("plugin://plugin.video.kofin.apiresearch/")
]


class Monitor(xbmc.Monitor):
    def __init__(self):
        super().__init__()
        self.events = []

    def onNotification(self, sender, method, data):
        if method != "VideoLibrary.OnUpdate":
            return
        payload = json.loads(data)
        item = payload.get("item", payload)
        if item.get("type") == "movie" and item.get("id") in ids:
            self.events.append({"sender": sender, "method": method, "data": payload})


monitor = Monitor()
before = rpc(
    "VideoLibrary.GetMovieDetails",
    {"movieid": ids[0], "properties": ["playcount", "resume"]},
)
userdata = rpc(
    "VideoLibrary.SetMovieDetails",
    {
        "movieid": ids[0],
        "playcount": 9,
        "lastplayed": "2020-02-03 04:05:06",
        "resume": {"position": 56.0, "total": 120.0},
    },
)
monitor.waitForAbort(0.2)
notifications = list(monitor.events)
after = rpc(
    "VideoLibrary.GetMovieDetails",
    {"movieid": ids[0], "properties": ["playcount", "resume"]},
)

benchmarks = []
for mode, size in [("individual", 1), ("batch25", 25), ("batch100", 100)]:
    samples = []
    for repeat in range(3):
        requests = [
            {
                "jsonrpc": "2.0",
                "id": index + 1,
                "method": "VideoLibrary.SetMovieDetails",
                "params": {
                    "movieid": mid,
                    "plot": "Synthetic benchmark {} {}".format(mode, repeat),
                },
            }
            for index, mid in enumerate(ids)
        ]
        t = time.perf_counter()
        errors = 0
        for start in range(0, len(requests), size):
            group = requests[start : start + size]
            if mode == "individual":
                response = json.loads(xbmc.executeJSONRPC(json.dumps(group[0])))
                errors += int("error" in response)
            else:
                errors += sum("error" in r for r in rpc(group))
        samples.append({"seconds": time.perf_counter() - t, "errors": errors})
    benchmarks.append({"mode": mode, "items": len(ids), "samples": samples})

batch = rpc(
    [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "VideoLibrary.SetMovieDetails",
            "params": {"movieid": ids[0], "plot": "Batch preceding failure"},
        },
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "VideoLibrary.SetMovieDetails",
            "params": {"movieid": 2147480000, "plot": "Missing item"},
        },
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "VideoLibrary.SetMovieDetails",
            "params": {"movieid": ids[1], "plot": "Batch after failure"},
        },
    ]
)
batch_readback = [
    rpc("VideoLibrary.GetMovieDetails", {"movieid": mid, "properties": ["plot"]})
    for mid in ids[:2]
]
monitor.waitForAbort(0.2)

result = {
    "transport": "xbmc.executeJSONRPC inside Kodi",
    "notifications": notifications,
    "userdata": {"before": before, "set": userdata, "after": after},
    "benchmarks": benchmarks,
    "partial_batch": {"response": batch, "readback": batch_readback},
    "notification_count_all_writes": len(monitor.events),
}
(root / "inprocess-result.json").write_text(json.dumps(result, indent=2))
