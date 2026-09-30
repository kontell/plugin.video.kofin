"""Standing orders mirror membership without deleting another owner's copy."""

import pytest

from kofin.core import ipc
from kofin.downloads import store, subscriptions
from kofin.sync import db as sync_db
from kofin.sync import newcontent


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
    assert wired == [(ipc.DOWNLOAD_REMOVE, {"Ids": ["song"], "Subscription": True})]


def test_manual_download_survives_playlist_departure(wired):
    owner = subscriptions.owner("playlist", "mix")
    store.queue(store.Download("song", media_type="song", origin=store.ORIGIN_USER))
    subscriptions.reconcile(owner, ["song"])
    subscriptions.reconcile(owner, [])
    assert wired == []


def test_playlist_remove_scope_excludes_manual_and_claimed_tracks(wired):
    store.queue(store.Download("requested", request_id="mix", origin=store.ORIGIN_USER))
    store.queue(store.Download("manual", origin=store.ORIGIN_USER))
    store.queue(store.Download("claimed", origin=subscriptions.owner("playlist", "mix")))
    subscriptions.reconcile(subscriptions.owner("musiclibrary", "library"), ["claimed"])
    assert store.playlist_unclaimed_states("mix") == {"requested": store.QUEUED}
    assert store.music_container_unclaimed_states("library", "musiclibrary") == {}


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


@pytest.mark.parametrize(
    "body",
    [{}, {"Items": [], "TotalRecordCount": 2}, {"Items": [None], "TotalRecordCount": 1}],
)
def test_incomplete_library_listing_keeps_existing_claims(wired, monkeypatch, body):
    owner = subscriptions.owner("musiclibrary", "library")
    subscriptions.reconcile(owner, ["song"])
    store.queue(store.Download("song", media_type="song", origin=owner))
    wired.clear()
    monkeypatch.setattr(
        subscriptions.settings, "get_str",
        lambda key: "library" if key == subscriptions.LIBRARY_SETTING else "",
    )

    class Api:
        def items(self, params):
            return body

    subscriptions.reconcile_music_libraries(Api())
    assert wired == []
    with sync_db.Database("kofin") as opened:
        opened.cursor.execute(
            "SELECT jellyfin_id FROM download_subscription WHERE owner = ?", (owner,)
        )
        assert opened.cursor.fetchall() == [("song",)]


def test_new_song_joins_its_library_subscription_after_writer_commit(wired, monkeypatch):
    monkeypatch.setattr(
        subscriptions.settings, "get_str",
        lambda key: "library" if key == subscriptions.LIBRARY_SETTING else "",
    )
    with sync_db.Database("kofin") as opened:
        opened.cursor.execute(
            "INSERT INTO jellyfin(jellyfin_id, media_folder) VALUES (?, ?)",
            ("song", "library"),
        )
    entry = newcontent.Entry("Audio", "song", "Song")
    subscriptions.claim_new_library_songs([entry])
    assert wired == [
        (
            ipc.DOWNLOAD_ADD,
            {"Ids": ["song"], "Types": ["Audio"], "Origin": "auto:musiclibrary:library"},
        )
    ]
    store.queue(store.Download("song", media_type="song", origin="auto:musiclibrary:library"))
    subscriptions.claim_new_library_songs([entry])
    assert len(wired) == 1
    with sync_db.Database("kofin") as opened:
        opened.cursor.execute("SELECT COUNT(*) FROM download_subscription")
        assert opened.cursor.fetchone()[0] == 1


def test_playlist_subscription_runs_without_playlist_file_sync(wired, monkeypatch):
    monkeypatch.setattr(
        subscriptions.settings, "get_str",
        lambda key: "mix" if key == subscriptions.PLAYLIST_SETTING else "",
    )

    class Api:
        def item(self, item_id):
            return {"Id": item_id, "MediaType": "Audio"}

        def playlist_items(self, item_id, start_index=0, limit=100):
            return {"Items": [{"Id": "song", "Type": "Audio"}], "TotalRecordCount": 1}

    subscriptions.reconcile_playlist_direct(Api(), "mix")
    assert wired == [
        (
            ipc.DOWNLOAD_ADD,
            {"Ids": ["song"], "Types": ["Audio"], "Origin": "auto:playlist:mix"},
        )
    ]


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
