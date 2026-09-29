"""Standing orders mirror membership without deleting another owner's copy."""

import pytest

from kofin.core import ipc
from kofin.downloads import store, subscriptions
from kofin.sync import db as sync_db


@pytest.fixture
def wired(tmp_path, monkeypatch):
    sync_db.reset_overrides()
    sync_db.set_path_override("kofin", str(tmp_path / "kofin.db"))
    monkeypatch.setattr(subscriptions.settings, "get_bool", lambda key: True)
    sent = []
    monkeypatch.setattr(
        subscriptions.ipc, "notify", lambda method, body: sent.append((method, body))
    )
    yield sent
    sync_db.reset_overrides()


def test_overlapping_subscriptions_remove_only_after_last_claim(wired):
    a = subscriptions.owner("playlist", "a")
    b = subscriptions.owner("playlist", "b")
    subscriptions.reconcile(a, ["song"])
    assert wired[-1][0] == ipc.DOWNLOAD_ADD
    store.queue(store.Download("song", media_type="song", origin=a))

    wired.clear()
    subscriptions.reconcile(b, ["song"])
    subscriptions.reconcile(a, [])
    assert wired == []

    subscriptions.reconcile(b, [])
    assert wired == [(ipc.DOWNLOAD_REMOVE, {"Ids": ["song"]})]


def test_manual_download_survives_playlist_departure(wired):
    owner = subscriptions.owner("playlist", "mix")
    store.queue(store.Download("song", media_type="song", origin=store.ORIGIN_USER))
    subscriptions.reconcile(owner, ["song"])
    subscriptions.reconcile(owner, [])
    assert wired == []


def test_stopping_subscription_keeps_existing_download(wired):
    owner = subscriptions.owner("musiclibrary", "library")
    subscriptions.reconcile(owner, ["song"])
    store.queue(store.Download("song", media_type="song", origin=owner))
    wired.clear()
    subscriptions.release(owner)
    assert store.get("song") is not None
    assert wired == []


def test_repeated_library_page_never_becomes_a_deletion_snapshot():
    class Api:
        def items(self, params):
            return {"Items": [{"Id": "same"}], "TotalRecordCount": 1000}

    with pytest.raises(ValueError, match="repeated"):
        subscriptions._library_items(Api(), "library")


def test_library_pages_continue_when_server_omits_total_count():
    class Api:
        def items(self, params):
            start = params["StartIndex"]
            return {
                "Items": [
                    {"Id": "song-%d" % index, "Type": "Audio"}
                    for index in range(start, min(start + params["Limit"], 201))
                ],
                "TotalRecordCount": 0,
            }

    assert len(subscriptions._library_items(Api(), "library")) == 201


def test_music_library_poll_mirrors_changes_without_bulk_album_gate(wired, monkeypatch):
    monkeypatch.setattr(
        subscriptions.settings,
        "get_str",
        lambda key: "library" if key == subscriptions.LIBRARY_SETTING else "",
    )
    monkeypatch.setattr(
        subscriptions.settings,
        "get_int",
        lambda key: (_ for _ in ()).throw(AssertionError("bulk cap consulted")),
    )

    class Api:
        ids = ["a", "b", "c"]

        def items(self, params):
            rows = [
                {"Id": item_id, "Type": "Audio", "CanDownload": True}
                for item_id in self.ids
            ]
            start = params["StartIndex"]
            return {
                "Items": rows[start : start + params["Limit"]],
                "TotalRecordCount": len(rows),
            }

    api = Api()
    subscriptions.reconcile_music_libraries(api)
    adds = [body for method, body in wired if method == ipc.DOWNLOAD_ADD]
    assert len(adds) == 1
    assert set(adds[0]["Ids"]) == {"a", "b", "c"}
    for item_id in api.ids:
        store.queue(
            store.Download(
                item_id,
                media_type="song",
                origin=subscriptions.owner("musiclibrary", "library"),
            )
        )

    wired.clear()
    api.ids = ["b", "c", "d"]
    subscriptions.reconcile_music_libraries(api)
    assert [body["Ids"] for method, body in wired if method == ipc.DOWNLOAD_ADD] == [
        ["d"]
    ]
    assert [body["Ids"] for method, body in wired if method == ipc.DOWNLOAD_REMOVE] == [
        ["a"]
    ]
