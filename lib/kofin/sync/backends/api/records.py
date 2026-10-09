"""The pass's view of a catalogue member and the window its payloads come through.

``Record`` is one member of the desired view with its payload read on first
use; ``PayloadWindow`` reads those payloads a chunk at a time and holds a
bounded number, so a pass over thousands of records never holds the
catalogue (200 MB of Python for 6,600 video items wedged a 1 GB device).
Lifted out of ``store`` so that module stays about the store.
"""

from collections import OrderedDict
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterable, Iterator, Optional

if TYPE_CHECKING:
    from .store import Store


class Record:
    """One member of the desired view.

    ``item`` is the payload, read on first use when the record was built
    without one. A pass over a whole catalogue holds a record for every
    pending item, and their payloads together weigh 3.5 times their JSON
    (200 MB for 6,600 video items): held all at once they wedged a 1 GB
    device. The pass reads them through a ``PayloadWindow`` instead.
    """

    __slots__ = (
        "item_id",
        "kind",
        "library",
        "parent_id",
        "generation",
        "_item",
        "_loader",
    )

    def __init__(
        self,
        item_id: str,
        kind: str,
        library: str,
        parent_id: str,
        item: Optional[Dict[str, Any]],
        generation: int,
        loader: Optional[Callable[[str], Dict[str, Any]]] = None,
    ):
        self.item_id = item_id
        self.kind = kind
        self.library = library
        self.parent_id = parent_id
        self.generation = generation
        self._item = item
        self._loader = loader

    @property
    def item(self) -> Dict[str, Any]:
        if self._item is not None:
            return self._item
        if self._loader is None:
            return {}
        return self._loader(self.item_id)

    @property
    def loaded(self) -> bool:
        return self._item is not None

    def _key(self):
        return (self.item_id, self.kind, self.library, self.parent_id, self.generation)

    def __eq__(self, other):
        if not isinstance(other, Record):
            return NotImplemented
        return self._key() == other._key() and self.item == other.item

    def __hash__(self):
        return hash(self._key())

    def __repr__(self):
        return "Record(%r, %r, generation=%r)" % (
            self.item_id,
            self.kind,
            self.generation,
        )


class PayloadWindow:
    """Payloads read on demand, a bounded number held at a time.

    ``walk`` yields records in order and reads each chunk's payloads with
    one query ahead of it, so a pass over thousands of records holds a few
    hundred payloads, never the catalogue.
    """

    CHUNK = 200

    def __init__(self, store: "Store", size: int = 512):
        self.store = store
        self.size = size
        self._held: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.reads = 0

    def __call__(self, item_id: str) -> Dict[str, Any]:
        payload = self._held.get(item_id)
        if payload is None:
            self.reads += 1
            payload = self.store.payload(item_id)
            self._keep(item_id, payload)
        else:
            self._held.move_to_end(item_id)
        return payload

    def _keep(self, item_id: str, payload: Dict[str, Any]):
        self._held[item_id] = payload
        while len(self._held) > self.size:
            self._held.popitem(last=False)

    def prefetch(self, item_ids: Iterable[str]):
        wanted = [i for i in item_ids if i not in self._held]
        if wanted:
            self.reads += 1
            for item_id, payload in self.store.payloads(wanted).items():
                self._keep(item_id, payload)

    def walk(self, records: Iterable[Record]) -> Iterator[Record]:
        pending = list(records)
        for start in range(0, len(pending), self.CHUNK):
            chunk = pending[start : start + self.CHUNK]
            unloaded = [r for r in chunk if not r.loaded]
            for record in unloaded:
                # A lazy record from ``records(payloads=False)`` reads one
                # row at a time; walked, it reads through this window.
                record._loader = self
            self.prefetch(r.item_id for r in unloaded)
            for record in chunk:
                yield record
