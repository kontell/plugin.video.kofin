"""The ``mode=audiotracks`` plugin entry: mid-play TC audio picker (PR5).

The plugin process cannot own the player session, so this only validates that
a pick makes sense and notifies the service. The service shows Dialog.select
on a worker thread and restarts the Transcode session via apply_stream_switch.
"""

from kofin.core import ipc, state, toast
from kofin.core.log import Logger
from kofin.plugin.router import Request

LOG = Logger(__name__)


def pick(request: Request) -> None:
    """IPC hand-off for the local Transcode audio-track fallback."""
    del request  # mode entry has no params; service uses the claimed session
    if not state.get_playing_id():
        LOG.info("audiotracks: nothing playing")
        toast.show(
            _nothing_playing_msg(),
            toast.WARNING,
            time_ms=3000,
        )
        return
    if not state.is_playing_pick_audio():
        # DirectStream multi-audio is native OSD; single-track TC has nothing
        # to pick. Context menu should already hide; mode can be keymapped.
        LOG.info("audiotracks: pick not offered for current session")
        toast.show(
            _nothing_playing_msg(),
            toast.WARNING,
            time_ms=3000,
        )
        return
    if state.is_syncplay_active():
        toast.show(
            "Stream changes are disabled while SyncPlay is active",
            toast.WARNING,
            time_ms=3000,
        )
        return

    LOG.debug("requesting TC audio track pick from the service")
    ipc.notify(ipc.PICK_AUDIO_TRACK)


def _nothing_playing_msg() -> str:
    from kofin.core import settings

    return settings.localized(30149)
