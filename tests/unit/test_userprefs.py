"""L1 units for the Jellyfin account settings dialog: what each row renders,
what a write sends, and what happens when the server refuses it."""

import re

import pytest

from kofin.core.http import HttpError, ServerUnreachable
from kofin.plugin import userprefs
from kofin.plugin.router import Request
from tests.unit.fakes import FakeAddon

# resources/settings.xml's label for honourJellyfinDefaultTracks, which the
# caveat row has to quote verbatim.
STR_DEFAULT_TRACKS_LABEL = 30618

CULTURES = [
    {
        "Name": "English",
        "DisplayName": "English",
        "TwoLetterISOLanguageName": "en",
        "ThreeLetterISOLanguageName": "eng",
    },
    {
        "Name": "Japanese",
        "DisplayName": "Japanese",
        "TwoLetterISOLanguageName": "ja",
        "ThreeLetterISOLanguageName": "jpn",
    },
    # A culture the server lists without a three-letter code: that code is the
    # stored value, so this one cannot be offered as a preference.
    {"Name": "Klingon", "DisplayName": "Klingon", "TwoLetterISOLanguageName": "tlh"},
]

# The shape /Users/Me returns, including the ten keys this dialog never edits.
CONFIG = {
    "AudioLanguagePreference": "jpn",
    "PlayDefaultAudioTrack": True,
    "SubtitleLanguagePreference": "eng",
    "SubtitleMode": "Smart",
    "RememberAudioSelections": True,
    "RememberSubtitleSelections": False,
    "OrderedViews": ["view-a", "view-b"],
    "GroupedFolders": ["folder-a"],
    "LatestItemsExcludes": [],
    "MyMediaExcludes": ["hidden-a"],
    "CastReceiverId": "F007D354",
    "DisplayMissingEpisodes": False,
    "HidePlayedInLatest": True,
    "DisplayCollectionsView": False,
    "EnableLocalPassword": False,
    "EnableNextEpisodeAutoPlay": True,
}


@pytest.fixture(autouse=True)
def addon(monkeypatch):
    FakeAddon.store = {}
    monkeypatch.setattr("xbmcaddon.Addon", FakeAddon)
    return FakeAddon


def _field(key):
    return next(field for field in userprefs.FIELDS if field.key == key)


# --- the pure helpers --------------------------------------------------------


def test_updated_carries_every_untouched_key_through():
    """/Users/Configuration replaces the whole document, so a write built from
    only the six fields this dialog knows about would clear the account's home
    screen. This is the function that stops it."""
    changed = userprefs.updated(CONFIG, "SubtitleMode", "OnlyForced")

    assert changed["SubtitleMode"] == "OnlyForced"
    assert set(changed) == set(CONFIG)
    for key, value in CONFIG.items():
        if key != "SubtitleMode":
            assert changed[key] == value, key


def test_updated_leaves_the_original_alone():
    """The caller keeps the old dict to revert to when a save fails."""
    userprefs.updated(CONFIG, "SubtitleMode", "None")

    assert CONFIG["SubtitleMode"] == "Smart"


def test_language_options_offer_any_first_and_drop_codeless_cultures():
    options = userprefs.language_options(CULTURES)

    assert options[0] == ("", "string-30788")  # "Any"
    assert options[1:] == [("eng", "English"), ("jpn", "Japanese")]


def test_language_label_falls_back_to_the_raw_code():
    """A code another client set against a different culture table is still
    the truth about the account; rendering it as "Any" would be a lie the user
    would have to overwrite to discover."""
    assert userprefs.language_label("eng", CULTURES) == "English"
    assert userprefs.language_label("", CULTURES) == "string-30788"
    assert userprefs.language_label("tlh", CULTURES) == "tlh"


def test_row_labels_render_each_kind():
    rows = userprefs.row_labels(CONFIG, CULTURES, honoured=True)

    assert rows == [
        "string-30780: Japanese",  # language
        "string-30781: string-30786",  # bool, Enabled
        "string-30782: English",
        "string-30783: string-30793",  # enum, Smart
        "string-30784: string-30786",
        "string-30785: string-30787",  # bool, Disabled
    ]


def test_row_labels_render_an_absent_language_as_any():
    """A never-set preference comes back absent, not empty."""
    config = dict(CONFIG)
    del config["AudioLanguagePreference"]

    rows = userprefs.row_labels(config, CULTURES, honoured=True)

    assert rows[0] == "string-30780: string-30788"


def test_row_labels_render_an_unknown_subtitle_mode_verbatim():
    rows = userprefs.row_labels(
        userprefs.updated(CONFIG, "SubtitleMode", "Whatever"), CULTURES, honoured=True
    )

    assert rows[3] == "string-30783: Whatever"


def test_the_caveat_row_appears_only_when_kofin_ignores_the_settings():
    """With honourJellyfinDefaultTracks off nothing here reaches playback on
    this box, and a dialog that appears to work and does nothing is worse than
    a blunt line of text."""
    honoured = userprefs.row_labels(CONFIG, CULTURES, honoured=True)
    ignored = userprefs.row_labels(CONFIG, CULTURES, honoured=False)

    assert len(honoured) == len(userprefs.FIELDS)
    assert ignored == honoured + ["string-30794"]


def test_every_field_label_and_option_has_a_string():
    """The ids the table names must exist in strings.po, or the row renders as
    a bare colon with no clue which setting it is. test_settings.py covers the
    button; these ids are named in code, so nothing else would."""
    with open(
        "resources/language/resource.language.en_gb/strings.po", encoding="utf-8"
    ) as handle:
        known = {int(found) for found in re.findall(r'msgctxt "#(\d+)"', handle.read())}

    wanted = {field.label for field in userprefs.FIELDS}
    for field in userprefs.FIELDS:
        wanted.update(label for _, label in field.options)
    wanted.update(
        {
            userprefs.STR_NO_SESSION,
            userprefs.STR_HEADING,
            userprefs.STR_ENABLED,
            userprefs.STR_DISABLED,
            userprefs.STR_ANY,
            userprefs.STR_NOT_HONOURED,
            userprefs.STR_LOAD_FAILED,
            userprefs.STR_SAVE_FAILED,
            userprefs.STR_NO_PERMISSION,
        }
    )

    assert wanted <= known, sorted(wanted - known)


def test_the_caveat_names_the_setting_the_way_the_playback_tab_labels_it():
    """The caveat row tells the user which switch to go and find, so it has to
    quote that switch's own label. It shipped saying "Honour Jellyfin default
    tracks" while the Playback tab calls it "Use Jellyfin's default tracks" --
    caught on a real box, and nothing else would have: both strings resolve,
    both render, and only a person hunting the settings tree finds out."""
    with open(
        "resources/language/resource.language.en_gb/strings.po", encoding="utf-8"
    ) as handle:
        strings = dict(
            re.findall(r'msgctxt "#(\d+)"\nmsgid "((?:[^"\\]|\\.)*)"', handle.read())
        )

    setting_label = strings[str(STR_DEFAULT_TRACKS_LABEL)].replace('\\"', '"')
    caveat = strings[str(userprefs.STR_NOT_HONOURED)].replace('\\"', '"')

    assert setting_label == "Use Jellyfin's default tracks"
    assert '"%s"' % setting_label in caveat, caveat


def test_fields_cover_the_agreed_configuration_keys():
    """The six in scope, and nothing that would desync the Kodi database or
    duplicate a kofin setting (see the module docstring)."""
    assert [field.key for field in userprefs.FIELDS] == [
        "AudioLanguagePreference",
        "PlayDefaultAudioTrack",
        "SubtitleLanguagePreference",
        "SubtitleMode",
        "RememberAudioSelections",
        "RememberSubtitleSelections",
    ]


def test_subtitle_mode_offers_the_servers_own_enum():
    """The values are the server's SubtitlePlaybackMode enum verbatim — a typo
    here is a rejected write, not something this end could catch."""
    assert [value for value, _ in _field("SubtitleMode").options] == [
        "Default",
        "Always",
        "OnlyForced",
        "None",
        "Smart",
    ]


# --- the dialog --------------------------------------------------------------


class FakeDialog:
    """Answers each select() from a script, recording what it was shown."""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.selects = []
        self.notifications = []

    def select(self, heading, choices, **kwargs):
        self.selects.append((heading, list(choices), kwargs.get("preselect")))
        return self.answers.pop(0) if self.answers else -1

    def notification(self, heading, message, *args, **kwargs):
        self.notifications.append(message)


class FakeApi:
    """Stands in for Api; ``fail`` refuses the write the way a 403 does."""

    user_id = "uid"

    def __init__(self, config=None, policy=None, fail=False):
        self.config = dict(CONFIG if config is None else config)
        self.policy = {"EnableUserPreferenceAccess": True} if policy is None else policy
        self.fail = fail
        self.posted = []

    def me(self):
        return {"Configuration": dict(self.config), "Policy": dict(self.policy)}

    def cultures(self):
        return CULTURES

    def update_user_configuration(self, configuration):
        if self.fail:
            raise HttpError(403, "POST /Users/Configuration -> 403")
        self.posted.append(dict(configuration))


def _logged_in():
    creds = userprefs.Credentials(user_id="uid", token="t", device_id="d")
    creds.display_user = "kofin-test"
    creds.is_logged_in = True
    return creds


def _request():
    return Request("plugin://plugin.video.kofin/", -1, {"mode": "userprefs"})


def _localized(string_id):
    """FakeAddon's "string-%d" with the one placeholder-bearing id spelled out:
    the heading names the user, and % over a string with no placeholder is a
    TypeError rather than a no-op."""
    if string_id == userprefs.STR_HEADING:
        return "Jellyfin settings for %s"
    return "string-%d" % string_id


@pytest.fixture
def dialog(monkeypatch):
    fake = FakeDialog()
    monkeypatch.setattr(userprefs.xbmcgui, "Dialog", lambda: fake)
    monkeypatch.setattr(userprefs.state, "is_online", lambda: True)
    monkeypatch.setattr(userprefs.settings, "localized", _localized)
    monkeypatch.setattr(
        userprefs.Credentials, "load", classmethod(lambda cls: _logged_in())
    )
    return fake


def _serving(monkeypatch, api):
    monkeypatch.setattr(
        userprefs.Api, "from_credentials", staticmethod(lambda *a, **k: api)
    )
    return api


def test_save_posts_the_whole_document_and_returns_it():
    api = FakeApi()

    result = userprefs._save(api, CONFIG, _field("SubtitleMode"), "OnlyForced")

    assert api.posted == [userprefs.updated(CONFIG, "SubtitleMode", "OnlyForced")]
    assert api.posted[0]["OrderedViews"] == ["view-a", "view-b"]
    assert result["SubtitleMode"] == "OnlyForced"


def test_save_keeps_the_old_configuration_when_the_server_refuses(dialog):
    """The menu must never show a value the server did not take: the next
    redraw is built from whatever this returns."""
    result = userprefs._save(FakeApi(fail=True), CONFIG, _field("SubtitleMode"), "None")

    assert result == CONFIG
    assert result["SubtitleMode"] == "Smart"
    assert dialog.notifications == ["string-30796"]


def test_ask_flips_a_boolean_without_a_second_dialog(dialog):
    assert userprefs._ask(_field("PlayDefaultAudioTrack"), CONFIG, CULTURES) is False
    assert dialog.selects == []


def test_ask_opens_an_enum_on_its_current_value(dialog):
    dialog.answers = [2]  # OnlyForced

    chosen = userprefs._ask(_field("SubtitleMode"), CONFIG, CULTURES)

    heading, choices, preselect = dialog.selects[0]
    assert heading == "string-30783"
    assert preselect == 4  # Smart, where the account already is
    assert choices[2] == "string-30791"
    assert chosen == "OnlyForced"


def test_ask_preselects_any_for_a_language_that_was_never_set(dialog):
    dialog.answers = [1]  # English
    config = dict(CONFIG)
    del config["AudioLanguagePreference"]

    chosen = userprefs._ask(_field("AudioLanguagePreference"), config, CULTURES)

    assert dialog.selects[0][2] == 0  # the "Any" row
    assert chosen == "eng"


def test_ask_returns_none_when_the_sub_dialog_is_backed_out_of(dialog):
    dialog.answers = [-1]

    assert userprefs._ask(_field("SubtitleMode"), CONFIG, CULTURES) is None


def test_the_menu_saves_each_change_and_redraws_with_it(dialog, monkeypatch):
    api = _serving(monkeypatch, FakeApi())
    FakeAddon.store["honourJellyfinDefaultTracks"] = "true"
    # Row 3 (Subtitle mode) -> option 3 (None); then row 1, a bool, toggles.
    dialog.answers = [3, 3, 1, -1]

    userprefs.jellyfin_settings(_request())

    assert [posted["SubtitleMode"] for posted in api.posted] == ["None", "None"]
    assert [posted["PlayDefaultAudioTrack"] for posted in api.posted] == [True, False]
    # selects is [menu, subtitle-mode sub-dialog, menu, menu] — the enum costs
    # a slot, the bool does not. The last draw carries both changes.
    assert dialog.selects[3][1][3] == "string-30783: string-30792"
    assert dialog.selects[3][1][1] == "string-30781: string-30787"


def test_the_menu_returns_the_cursor_to_the_row_just_edited(dialog, monkeypatch):
    _serving(monkeypatch, FakeApi())
    FakeAddon.store["honourJellyfinDefaultTracks"] = "true"
    dialog.answers = [3, 1, -1]

    userprefs.jellyfin_settings(_request())

    assert dialog.selects[0][0] == "Jellyfin settings for kofin-test"
    assert dialog.selects[0][2] == 0
    assert dialog.selects[1][2] == 4  # the sub-dialog, on the current mode
    assert dialog.selects[2][2] == 3  # the menu again, back on Subtitle mode


def test_the_menu_writes_nothing_when_the_value_did_not_change(dialog, monkeypatch):
    """Re-picking what is already set is not worth a round trip."""
    api = _serving(monkeypatch, FakeApi())
    dialog.answers = [3, 4, -1]  # Subtitle mode -> Smart, which it already is

    userprefs.jellyfin_settings(_request())

    assert api.posted == []


def test_the_caveat_row_is_a_caption_not_a_choice(dialog, monkeypatch):
    """Selecting it must redraw, never index past the field table."""
    api = _serving(monkeypatch, FakeApi())
    FakeAddon.store["honourJellyfinDefaultTracks"] = "false"
    dialog.answers = [len(userprefs.FIELDS), -1]

    userprefs.jellyfin_settings(_request())

    assert api.posted == []
    assert len(dialog.selects) == 2


def test_the_menu_refuses_to_open_without_preference_access(dialog, monkeypatch):
    """A 403 on the first write is indistinguishable from a signed-out 401
    once the transport has folded them together, so this is said up front."""
    _serving(monkeypatch, FakeApi(policy={"EnableUserPreferenceAccess": False}))

    userprefs.jellyfin_settings(_request())

    assert dialog.selects == []
    assert dialog.notifications == ["string-30797"]


def test_the_menu_opens_when_the_policy_does_not_mention_the_flag(dialog, monkeypatch):
    """Absent is not false: let the server refuse rather than pre-empting it
    on a DTO shape that changed."""
    _serving(monkeypatch, FakeApi(policy={}))

    userprefs.jellyfin_settings(_request())

    assert len(dialog.selects) == 1


def test_the_menu_says_so_when_the_load_fails(dialog, monkeypatch):
    class DeadApi(FakeApi):
        def me(self):
            raise ServerUnreachable("GET /Users/Me: down")

    _serving(monkeypatch, DeadApi())

    userprefs.jellyfin_settings(_request())

    assert dialog.selects == []
    assert dialog.notifications == ["string-30795"]


def test_the_menu_does_nothing_when_signed_out(dialog, monkeypatch):
    monkeypatch.setattr(
        userprefs.Credentials, "load", classmethod(lambda cls: userprefs.Credentials())
    )

    userprefs.jellyfin_settings(_request())

    assert dialog.selects == []
    assert dialog.notifications == []


def test_the_menu_says_so_when_offline(dialog, monkeypatch):
    monkeypatch.setattr(userprefs.state, "is_online", lambda: False)

    userprefs.jellyfin_settings(_request())

    assert dialog.selects == []
    assert dialog.notifications == ["string-30045"]
