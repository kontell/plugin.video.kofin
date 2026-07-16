"""Service lifecycle: build, run, and rebuild on soft restart.

The outer loop owns restarts — a restart tears the Service object down and
builds a fresh one. Nothing may survive a cycle at module level; all state
lives on the objects rebuilt each pass.
"""

import xbmc

from kofin.core import ipc, state
from kofin.core.log import Logger
from kofin.core.settings import Credentials, addon_version

LOG = Logger(__name__)


class Service(xbmc.Monitor):
    def __init__(self) -> None:
        super().__init__()
        self._restart_requested = False
        self.credentials = Credentials.load()

    def onNotification(self, sender: str, method: str, data: str) -> None:
        if sender != ipc.SENDER:
            return
        name = ipc.method_name(method)
        if name == ipc.RESTART:
            LOG.info("restart requested")
            self._restart_requested = True
        elif name == ipc.AUTH_CHANGED:
            LOG.info("auth changed; restarting service cycle")
            self.credentials = Credentials.load()
            self._restart_requested = True

    def run(self) -> bool:
        """Run until abort or restart; returns True when a rebuild is wanted."""
        LOG.info("--->>> kofin service %s", addon_version())
        LOG.info("kodi %s", xbmc.getInfoLabel("System.BuildVersion"))
        try:
            while not self.abortRequested():
                if self._restart_requested:
                    break
                if self.waitForAbort(1):
                    break
        finally:
            self._shutdown()
        LOG.info("---<<< kofin service")
        return self._restart_requested and not self.abortRequested()

    def _shutdown(self) -> None:
        state.clear_all()


def run_forever() -> None:
    while True:
        service = Service()
        if not service.run():
            break
        del service
