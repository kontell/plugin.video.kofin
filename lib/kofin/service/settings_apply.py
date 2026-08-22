"""The settings diff engine: a registry of ``setting id -> handler(old, new)``
consulted from the service's ``onSettingsChanged`` (plan §2).

Phase 1's inline sslVerify handler lives here now. Phase 2 adds
``librarySelection``: the whitelist csv written by the library picker. Its
handler computes add/remove sets against the *synced* whitelist (sync.json)
— not the previous csv — so a partially failed sync self-heals on the next
apply. Removals confirm via yesno before rows are deleted; a declined
removal restores the ids into the selection.

Startup guard (learned in S2 live testing): Kodi fires ``onSettingsChanged``
while it loads the profile settings, and the fresh-``Addon()``-per-call reads
(needed for reuselanguageinvoker correctness) can transiently return "" before
the persisted value lands. Acting on those transients once prompted the user
to *remove a synced library* on a plain Kodi restart. So the applier ignores
every change until the service marks it ready, then re-baselines against the
now-stable store. A genuine user edit only ever happens interactively, long
after startup.

The same read can fail *after* ready, though (phase 5 live testing: Kodi
logged "failed to load addon settings from ...settings.xml" four minutes into
a ready session and handed back "" for librarySelection, which proposed
removing all six synced libraries). So emptying a guarded setting is
corroborated before it is believed — by a re-read and by a canary setting that
is never legitimately empty, because the failure arrives in bursts and a
re-read on its own can land inside the same broken window. See
``_is_spurious_clear``.
"""

from typing import Any, Callable, Dict, List, Optional

import xbmcgui

from kofin.core import settings, state
from kofin.core.log import Logger

LOG = Logger(__name__)

Handler = Callable[[str, str], None]

# Settings whose emptied value destroys data if believed too readily, so an
# empty read is corroborated before it is acted on (``_is_spurious_clear``).
# ``syncMusicPlaylists`` is here too: a failed settings load during materialize
# was observed live as true→"" which fired CleanupMusicPlaylists and wiped
# the just-written ``playlists/music/Kofin/`` folder.
GUARDED_CLEARS = ("librarySelection", "syncMusicPlaylists")

# Non-empty for the whole life of an installed addon: Credentials.load
# generates it on first use and logging out deliberately keeps it. So an empty
# read of it never means "the user changed something" — it means the settings
# document did not load.
LOAD_CANARY = "deviceId"


class SettingsApplier:
    def __init__(self, service: object) -> None:
        self.service = service
        self.ready = False
        self.handlers: Dict[str, Handler] = {
            "sslVerify": self._ssl_verify_changed,
            "librarySelection": self._library_selection_changed,
            "syncPlayEnabled": self._syncplay_enabled_changed,
            "syncPlayTempo": self._syncplay_tempo_changed,
            "whoIsWatchingShortlist": self._who_shortlist_changed,
            "contextBitrates": self._context_bitrates_changed,
            "syncMusicPlaylists": self._sync_music_playlists_changed,
            "musicTranscode": self._music_transcode_changed,
            "useServerBackdrop": self._server_backdrop_changed,
            "preferCriticRating": self._prefer_critic_rating_changed,
            "downloadsEnabled": self._downloads_enabled_changed,
            "downloadsPath": self._downloads_path_changed,
        }
        self.snapshot: Dict[str, str] = self._read_all()

    def _read_all(self) -> Dict[str, str]:
        return {
            setting_id: settings.get_str(setting_id) for setting_id in self.handlers
        }

    def mark_ready(self) -> None:
        """Start honoring settings changes, re-baselining against the settings
        store now that startup's transient reads are over. Idempotent."""
        if self.ready:
            return
        self.snapshot = self._read_all()
        self.ready = True
        # Publish rather than wait for a change: the plugin process reads this
        # property on every context-menu draw, including the first.
        state.set_context_bitrates(self.snapshot.get("contextBitrates", ""))
        # Same reason: skins cannot read the settings that hide these two
        # root entries, so the service mirrors the offer onto properties.
        self._publish_root_menus()
        LOG.debug("settings applier ready; baseline re-read")

    def apply(self) -> None:
        """Run the handler for every watched setting whose value changed."""
        if not self.ready:
            # A startup transient, not a user edit — never act on it, or a
            # transient empty read of librarySelection looks like "user removed
            # every library" and prompts a destructive removal.
            LOG.debug("settings change before ready; ignored")
            return
        # Whole-document load failures blank every setting (or, for booleans
        # with a default of false, surface the default). Acting on that once
        # wiped managed music playlists live: true→false CleanupMusicPlaylists
        # mid-materialize. If the canary is empty, trust nothing this cycle.
        if settings.get_str(LOAD_CANARY) == "":
            LOG.warning(
                "settings document failed to load (%s empty); skipping apply cycle",
                LOAD_CANARY,
            )
            return
        for setting_id, handler in self.handlers.items():
            new = settings.get_str(setting_id)
            old = self.snapshot.get(setting_id, "")
            if new == old:
                continue
            if self._is_spurious_clear(setting_id, old, new):
                # Snapshot deliberately not advanced: the real value is still
                # pending, and a later genuine edit must still register.
                continue
            self.snapshot[setting_id] = new
            LOG.info("setting %s changed: %r -> %r; applying", setting_id, old, new)
            try:
                handler(old, new)
            except Exception:
                LOG.exception("apply failed for %s", setting_id)

    def _is_spurious_clear(self, setting_id: str, old: str, new: str) -> bool:
        """Whether an emptied setting is a failed read rather than an edit.

        The startup guard above covers transient empty reads *before* ready;
        this covers the same failure after it. Kodi can fail to load
        settings.xml mid-session ("failed to load addon settings from
        special://profile/addon_data/.../settings.xml") and hand back "" for
        a setting that is intact on disk — observed live during phase 5, four
        minutes into a ready session, which read as "user deselected every
        library" and prompted removal of all six.

        A re-read alone is not enough. The failure comes in bursts: Kodi logged
        it three times in a row while a repair was running, so the immediate
        re-read landed inside the same broken window and the removal prompt
        appeared anyway. The decisive test is a canary instead of a retry — a
        failed load empties *every* setting, not just this one, so a
        ``deviceId`` that reads empty proves the document did not parse. Both
        checks are single reads, so a genuine clear still proceeds without
        waiting on anything.
        """
        if setting_id not in GUARDED_CLEARS or new != "" or old == "":
            return False

        confirm = settings.get_str(setting_id)
        if confirm != new:
            LOG.warning(
                "ignoring spurious empty read of %s (re-read returned %r); "
                "treating it as a failed settings load, not a user edit",
                setting_id,
                confirm,
            )
            return True

        if settings.get_str(LOAD_CANARY) == "":
            LOG.warning(
                "ignoring empty read of %s: %s came back empty too, so the "
                "settings document failed to load rather than %s being cleared",
                setting_id,
                LOAD_CANARY,
                setting_id,
            )
            return True

        return False

    # -- handlers -------------------------------------------------------------

    def _ssl_verify_changed(self, old: str, new: str) -> None:
        LOG.info("sslVerify changed; restarting service cycle")
        self.service._restart_requested = True  # type: ignore[attr-defined]

    def _syncplay_enabled_changed(self, old: str, new: str) -> None:
        """The SyncPlay master toggle builds/tears down the manager live —
        off means no manager thread at all (plan §4)."""
        service = self.service
        if new == "true":
            if getattr(service, "_online", False):
                service._start_syncplay()  # type: ignore[attr-defined]
        else:
            service._stop_syncplay()  # type: ignore[attr-defined]
        self._publish_root_menus()

    def _syncplay_tempo_changed(self, old: str, new: str) -> None:
        """Fine sync arms at group join; a toggle while in a group takes
        effect now rather than at the next join."""
        manager = getattr(self.service, "syncplay", None)
        if manager is not None:
            manager.refresh_tempo_session()

    def _who_shortlist_changed(self, old: str, new: str) -> None:
        """The Advanced-tab shortlist is also how Who's watching? is
        switched off (the nobody sentinel). Republish so a skin button
        tracks the root entry without waiting for a service restart."""
        self._publish_root_menus()

    def _publish_root_menus(self) -> None:
        """Mirror addon-root Who's watching? / SyncPlay visibility for skins.

        Same gates as ``plugin.browse.root``: logged in, and the feature's
        own offer function. Written rather than derived in the skin because
        a ``<visible>`` cannot read an addon setting (core/state.py).
        """
        from kofin.core.settings import Credentials
        from kofin.plugin import adduser, syncplay

        logged_in = Credentials.load().is_logged_in
        state.set_menu_who(logged_in and adduser.is_enabled())
        state.set_menu_syncplay(logged_in and syncplay.available())

    def _context_bitrates_changed(self, old: str, new: str) -> None:
        """Keep the property addon.xml gates the transcode context item on."""
        state.set_context_bitrates(new)

    def _sync_music_playlists_changed(self, old: str, new: str) -> None:
        """Enable → materialize all; disable → delete managed Kofin/ folder."""
        library = self._library_manager()
        if library is None:
            LOG.warning("syncMusicPlaylists changed but library manager unavailable")
            return
        if new == "true":
            library.enqueue_command("SyncMusicPlaylists")
            return
        # Disable path is destructive (deletes playlists/music/Kofin/). Live
        # testing showed failed settings loads can surface the boolean default
        # ("false") while the document is mid-rewrite after setSettingBool —
        # corroborate before wiping.
        confirm = settings.get_str("syncMusicPlaylists")
        if settings.get_str(LOAD_CANARY) == "" or confirm == "true":
            LOG.warning(
                "ignoring unconfirmed syncMusicPlaylists off "
                "(confirm=%r, canary empty=%s); leaving managed playlists",
                confirm,
                settings.get_str(LOAD_CANARY) == "",
            )
            self.snapshot["syncMusicPlaylists"] = "true"
            return
        library.enqueue_command("CleanupMusicPlaylists")

    def _server_backdrop_changed(self, old: str, new: str) -> None:
        """Swap the addon fanart now rather than at the next connect.

        ``force`` because the daily fetch floor is there to stop reconnect
        churn, not to make a deliberate toggle wait a day. Off needs no server
        and no thread of its own, but goes the same way so the two directions
        cannot disagree about what is on disk.
        """
        service = self.service
        service._start_backdrop(force=True)  # type: ignore[attr-defined]

    def _music_transcode_changed(self, old: str, new: str) -> None:
        """Path mode flip rewrites MyMusic rows later; rematerialize playlists
        so lines match the new path form when playlist sync is on."""
        if not settings.get_bool("syncMusicPlaylists"):
            return
        library = self._library_manager()
        if library is None:
            return
        library.enqueue_command("SyncMusicPlaylists")

    def _prefer_critic_rating_changed(self, old: str, new: str) -> None:
        """Point already-synced films at the other rating row.

        No resync: both rows are written at sync time, so the flip is a
        pointer rewrite over MyVideos (``library.repoint_ratings``). Films
        that predate the option have no critic row yet and simply keep their
        community rating until a Repair fetches one — which is what the
        setting's help text asks for.
        """
        library = self._library_manager()
        if library is None:
            LOG.warning("preferCriticRating changed but library manager unavailable")
            return
        library.enqueue_command("RepointRatings")

    def _downloads_enabled_changed(self, old: str, new: str) -> None:
        """The downloads master toggle builds/tears down the manager live,
        the same shape as SyncPlay's (plan W1.1)."""
        service = self.service
        if new == "true":
            if getattr(service, "_online", False):
                service._start_downloads()  # type: ignore[attr-defined]
        else:
            service._stop_downloads()  # type: ignore[attr-defined]
        self._regenerate_nodes()

    def _regenerate_nodes(self) -> None:
        """Rewrite the generated library nodes now.

        The Downloaded nodes exist only while the feature is on, but nothing
        used to regenerate the tree when the toggle moved: node generation
        hangs off the library thread's startup and off library add/remove, so
        enabling downloads left the nodes to appear at the next service start
        — behind a server probe and two library calls, which is why they
        turned up about a minute later.

        Local work only (``get_nodes`` reads kofin.db and settings; ``Views``
        takes no server for this path), and contained: a node write must
        never take a settings apply down with it.

        "Local work only" was aspirational until ``window_nodes`` learned to
        check: it asked the server for the media-folder listing regardless,
        so every toggle of this setting logged two tracebacks on the way
        through. The listing feeds the library-tile artwork prop, which a
        serverless pass clears anyway.
        """
        try:
            from kofin.sync.views import Views

            Views().get_nodes()
        except Exception:
            LOG.exception("node regeneration after a settings change failed")

    def _downloads_path_changed(self, old: str, new: str) -> None:
        """Write-probe the new root now, not at the first download (W1.1).

        The probe is the whole apply: existing downloads deliberately stay
        where they are (migration is a stated phase-1 non-goal), and the
        manager reads the setting per download, so nothing needs a restart.
        """
        import os

        from kofin.downloads import downloads_root

        root = downloads_root()
        probe = os.path.join(root, ".kofin-write-probe")
        try:
            os.makedirs(root, exist_ok=True)
            with open(probe, "w") as handle:
                handle.write("probe")
            os.remove(probe)
        except OSError as error:
            LOG.warning("downloads path %r is not writable: %s", root, error)
            xbmcgui.Dialog().notification(
                "Kofin", settings.localized(30717), xbmcgui.NOTIFICATION_WARNING, 5000
            )
            return

        # The Downloaded-music view's one rule is a path under the old root;
        # re-aim it when the view exists (plan W3.3).
        from kofin.sync import playlists

        if os.path.isfile(
            os.path.join(playlists.managed_dir(), playlists.DOWNLOADED_MUSIC_XSP)
        ):
            playlists.refresh_downloaded_music()

        # The generated nodes filter on the root too — the Downloaded
        # episodes node by path, the music node by path — so they are as
        # stale as the .xsp was.
        self._regenerate_nodes()

    def _library_selection_changed(self, old: str, new: str) -> None:
        """The apply-on-save path for the library multiselect."""
        from kofin.sync import db as sync_db
        from kofin.sync import kofindb

        selection = {part for part in new.split(",") if part}

        sync = sync_db.get_sync()
        whitelist_entries = sync["Whitelist"]
        synced_ids = {entry.replace("Mixed:", "") for entry in whitelist_entries}

        additions = sorted(selection - synced_ids)
        removal_entries = [
            entry
            for entry in whitelist_entries
            if entry.replace("Mixed:", "") not in selection
        ]

        if not additions and not removal_entries:
            return

        if removal_entries:
            removal_entries = self._confirm_removals(removal_entries, selection)

        library = self._library_manager()
        if library is None:
            LOG.warning("library selection changed but sync manager unavailable")
            return

        if removal_entries:
            library.enqueue_command("RemoveLibrary", {"Id": ",".join(removal_entries)})

        if additions:
            library.enqueue_command("SyncLibrary", {"Id": ",".join(additions)})

    # -- plumbing -------------------------------------------------------------

    def _library_manager(self) -> Optional[Any]:
        service = self.service
        service._start_library()  # type: ignore[attr-defined]
        return getattr(service, "library", None)

    def _confirm_removals(
        self, removal_entries: List[str], selection: set
    ) -> List[str]:
        """Yes/no gate before rows are deleted; declined removals go back
        into librarySelection so the stored intent matches reality."""
        from kofin.sync import db as sync_db
        from kofin.sync import kofindb

        names = []
        with sync_db.Database("kofin") as opened:
            db = kofindb.JellyfinDatabase(opened.cursor)
            for entry in removal_entries:
                view = db.get_view(entry.replace("Mixed:", ""))
                names.append(view.view_name if view else entry)

        confirmed = xbmcgui.Dialog().yesno(
            settings.localized(30264),
            settings.localized(30265) % ", ".join(names),
        )
        if confirmed:
            return removal_entries

        restored = sorted(
            selection | {entry.replace("Mixed:", "") for entry in removal_entries}
        )
        restored_csv = ",".join(restored)
        self.snapshot["librarySelection"] = restored_csv
        settings.set_str("librarySelection", restored_csv)
        LOG.info("library removal declined; selection restored")
        return []
