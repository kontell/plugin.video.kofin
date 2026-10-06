"""Storage-independent decisions about metadata and userdata application."""

from dataclasses import dataclass
from typing import Any, Mapping, Optional


@dataclass(frozen=True)
class UpdateDecision:
    checksum: Optional[str]
    skip_metadata: bool
    apply_userdata: bool


def reference_checksum(etag: Any, direct_path: bool = False) -> Optional[str]:
    # A missing Etag must remain unverifiable, never become a reusable match.
    return "%s|%s" % (etag, "direct" if direct_path else "plugin") if etag else None


def plan_update(
    item: Mapping[str, Any],
    previous_checksum: Optional[str],
    *,
    exists: bool,
    direct_path: bool = False,
    force: bool = False,
    allow_userdata: bool = True,
) -> UpdateDecision:
    checksum = reference_checksum(item.get("Etag"), direct_path)
    skip = (
        not force and exists and checksum is not None and checksum == previous_checksum
    )
    return UpdateDecision(
        checksum,
        skip,
        skip and allow_userdata and bool(item.get("_userdata_changed", True)),
    )
