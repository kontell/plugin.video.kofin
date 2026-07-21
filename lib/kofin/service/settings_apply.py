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
corroborated by a second read before it is believed — see
``_is_spurious_clear``.
"""

from typing import Any, Callable, Dict, List, Optional

import xbmcgui

from kofin.core import settings
from kofin.core.log import Logger

LOG = Logger(__name__)

Handler = Callable[[str, str], None]

# Settings whose emptied value destroys data if believed too readily, so an
# empty read is corroborated before it is acted on (``_is_spurious_clear``).
GUARDED_CLEARS = ("librarySelection",)


class SettingsApplier:
    def __init__(self, service: object) -> None:
        self.service = service
        self.ready = False
        self.handlers: Dict[str, Handler] = {
            "sslVerify": self._ssl_verify_changed,
            "librarySelection": self._library_selection_changed,
            "syncPlayEnabled": self._syncplay_enabled_changed,
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
        LOG.debug("settings applier ready; baseline re-read")

    def apply(self) -> None:
        """Run the handler for every watched setting whose value changed."""
        if not self.ready:
            # A startup transient, not a user edit — never act on it, or a
            # transient empty read of librarySelection looks like "user removed
            # every library" and prompts a destructive removal.
            LOG.debug("settings change before ready; ignored")
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
        library" and prompted removal of all six. ``get_str`` builds a fresh
        ``Addon()`` per call, so the cheap discriminator is a second read:
        the failure is in one Addon instantiation and a new one lands the
        real value, while a genuine clear reads empty twice and proceeds.
        """
        if setting_id not in GUARDED_CLEARS or new != "" or old == "":
            return False

        confirm = settings.get_str(setting_id)
        if confirm == new:
            return False

        LOG.warning(
            "ignoring spurious empty read of %s (re-read returned %r); "
            "treating it as a failed settings load, not a user edit",
            setting_id,
            confirm,
        )
        return True

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
