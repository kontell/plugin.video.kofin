"""SyncPlay client for Kodi (phase 4 transplant of the fork's package).

Follows the SyncPlay protocol specification (docs/SYNCPLAY.md in the
jellyfin repository). Wiring:

- service/remote.py routes SyncPlayCommand / SyncPlayGroupUpdate websocket
  messages into SyncPlayManager.on_notification; service/main.py forwards
  WebSocketConnected (reconnects) and the SyncPlayMenu IPC.
- service/player.py forwards its playback callbacks into the manager, which
  distinguishes user intent from SyncPlay's own player actions.
"""

from kofin.syncplay.manager import SyncPlayManager
from kofin.syncplay.ui import show_menu

__all__ = ["SyncPlayManager", "show_menu"]
