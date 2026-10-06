"""Operations shared by catalogue policy and storage adapters.

Prepared means a transaction has accepted a write. Staged means desired state
is durable but Kodi has not confirmed it. Only an applied result is suitable
for new-content notifications. An exception leaves the operation owing a retry.
No cursor, connection or native schema crosses this boundary.
"""

from dataclasses import dataclass
from typing import Any, ContextManager, List, Literal, Optional, Protocol

from kofin.sync.model import MediaItem

Status = Literal["prepared", "staged", "applied", "skipped", "unsupported"]


@dataclass(frozen=True)
class ApplyResult:
    item: MediaItem
    status: Status
    new: bool = False
    outcome: Any = None

    @property
    def confirmed(self) -> bool:
        return self.status == "applied"


class Batch(Protocol):
    def apply(self, item: MediaItem) -> ApplyResult: ...

    def userdata(self, item: MediaItem) -> ApplyResult: ...

    def artwork(self, item: MediaItem) -> ApplyResult: ...

    def remove(self, item: MediaItem) -> ApplyResult: ...

    def commit(self) -> List[ApplyResult]: ...


class Backend(Protocol):
    name: str

    def batch(
        self,
        kind: str,
        server: Any,
        library: Optional[dict] = None,
        *,
        full_sync: bool = False,
        hooks: bool = True,
    ) -> ContextManager[Batch]: ...


def create_backend() -> Any:
    # Deliberately lazy: an OR process must never import the SQL implementation.
    from kofin.buildconfig import BACKEND

    if BACKEND == "sql":
        from kofin.sync.backends.sql.backend import SQLBackend

        return SQLBackend()
    if BACKEND == "api":
        from kofin.sync.backends.api import APIBackend

        return APIBackend()
    raise ValueError("unknown Kofin backend: %s" % BACKEND)


class CompatibilityError(Exception):
    """The selected adapter cannot use this native library."""
