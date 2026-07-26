"""L1 units for the settings-button actions: which dialogs a library action
puts in front of the user, and what it notifies the service."""

import pytest

from kofin.core import ipc
from kofin.plugin import actions


class FakeDialog:
    """Records every dialog raised, and answers from a canned script."""

    def __init__(self, multiselect=None, yesno=True):
        self.multiselect_result = multiselect
        self.yesno_result = yesno
        self.multiselects = []
        self.yesnos = []

    def multiselect(self, heading, choices, **kwargs):
        self.multiselects.append((heading, list(choices)))
        return self.multiselect_result

    def yesno(self, heading, message, **kwargs):
        self.yesnos.append((heading, message))
        return self.yesno_result


@pytest.fixture
def wired(monkeypatch):
    dialog = FakeDialog()
    notified = []

    monkeypatch.setattr(actions.xbmcgui, "Dialog", lambda: dialog)
    # Every string carries a placeholder: several of these ids take a
    # substitution, and a fake without one turns "the dialog was raised at
    # all" into a formatting error — which would pass for the wrong reason.
    monkeypatch.setattr(actions.settings, "localized", lambda i: "L%d %%s" % i)
    monkeypatch.setattr(actions.settings, "get_list", lambda key: ["lib1", "lib2"])
    monkeypatch.setattr(actions, "_selection_names", lambda ids: list(ids))
    monkeypatch.setattr(
        actions.ipc, "notify", lambda method, data=None: notified.append((method, data))
    )
    return dialog, notified


def test_repair_asks_nothing_beyond_the_picker(wired):
    """The confirmation here borrowed the *removal* copy — "Remove %s from the
    Kodi library? The items are deleted from this device only." — so repairing
    read as though it would leave the library gone. The picker is already the
    decision and a repair rebuilds from the server, so there is nothing
    destructive to confirm."""
    dialog, notified = wired
    dialog.multiselect_result = [1]  # first real library

    actions.repair_libraries(object())

    assert dialog.yesnos == []
    assert notified == [(ipc.REPAIR_LIBRARY, {"Id": "lib1"})]


def test_repair_all_selects_the_whole_whitelist(wired):
    dialog, notified = wired
    dialog.multiselect_result = [0]  # the "All" row

    actions.repair_libraries(object())

    assert dialog.yesnos == []
    assert notified == [(ipc.REPAIR_LIBRARY, {"Id": "lib1,lib2"})]


def test_repair_cancelled_notifies_nothing(wired):
    dialog, notified = wired
    dialog.multiselect_result = None

    actions.repair_libraries(object())

    assert notified == []


def test_repair_without_a_whitelist_does_nothing(wired, monkeypatch):
    dialog, notified = wired
    monkeypatch.setattr(actions.settings, "get_list", lambda key: [])

    actions.repair_libraries(object())

    assert dialog.multiselects == []
    assert notified == []


# --- delete from server ------------------------------------------------------


class DeletingApi:
    def __init__(self):
        self.deleted = []

    def delete_item(self, item_id):
        self.deleted.append(item_id)


@pytest.fixture
def delete_wired(wired, monkeypatch):
    dialog, _ = wired
    api = DeletingApi()
    toggles = {"enableDelete": True, "deleteNoConfirm": False}
    monkeypatch.setattr(actions, "_api", lambda: api)
    monkeypatch.setattr(
        actions.settings, "get_bool", lambda key: toggles.get(key, False)
    )
    monkeypatch.setattr(actions.xbmc, "executebuiltin", lambda command: None)
    return dialog, api, toggles


def _delete_request():
    return actions.Request(
        "plugin://plugin.video.kofin/", -1, {"id": "i1", "name": "X"}
    )


def test_delete_confirms_first(delete_wired):
    dialog, api, _ = delete_wired

    actions.delete_item(_delete_request())

    assert len(dialog.yesnos) == 1
    assert api.deleted == ["i1"]


def test_delete_declined_keeps_the_item(delete_wired):
    dialog, api, _ = delete_wired
    dialog.yesno_result = False

    actions.delete_item(_delete_request())

    assert api.deleted == []


def test_delete_without_confirmation_skips_the_prompt(delete_wired):
    """``deleteNoConfirm`` is scoped to the context-menu entry: picking Delete
    off a menu is already the deliberate act the prompt was guarding."""
    dialog, api, toggles = delete_wired
    toggles["deleteNoConfirm"] = True

    actions.delete_item(_delete_request())

    assert dialog.yesnos == []
    assert api.deleted == ["i1"]


def test_delete_stays_off_without_the_opt_in(delete_wired):
    dialog, api, toggles = delete_wired
    toggles["enableDelete"] = False
    toggles["deleteNoConfirm"] = True

    actions.delete_item(_delete_request())

    assert dialog.yesnos == []
    assert api.deleted == []
