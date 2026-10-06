"""Shared post-commit notification policy; adapters own their write hooks."""

from kofin.sync.backend import ApplyResult
from kofin.sync import newcontent


def announce(result: ApplyResult, output) -> None:
    if result.confirmed and result.new:
        entry = newcontent.entry_for(result.item.payload)
        if entry is not None:
            output.put(entry)
