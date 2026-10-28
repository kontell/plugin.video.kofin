"""The one spelling of a reference checksum."""

from typing import Any, Optional


def reference_checksum(etag: Any, direct_path: bool = False) -> Optional[str]:
    # A missing Etag must remain unverifiable, never become a reusable match.
    return "%s|%s" % (etag, "direct" if direct_path else "plugin") if etag else None
