"""The pass on a Piers: the baseline, every Kodi 23 feature off.

What a Kodi lacks is off by name, never an error (CLAUDE.md, the capability
profile): a property Kodi does not name is never asked for, a setter field
it does not take is never sent nor compared, and the same profile moved to
a Kodi that has them compares again and patches what Piers could not.
"""

from kofin.sync.backends.api import capabilities
from kofin.sync.backends.api.kinds import properties
from tests.unit.apifixtures import (  # noqa: F401
    LIB,
    backend,
    kodi,
    methods,
    piers,
    show_bundle,
    store,
)


def _requested(kodi, method):
    return [set(params.get("properties") or []) for params in methods(kodi, method)]


def test_the_readback_properties_follow_the_feature_table():
    assert "tag" not in properties("Episode", frozenset())
    assert "tag" in properties("Episode", frozenset({"episode_tags"}))
    assert "plot" not in properties("Season", frozenset())
    assert "plot" in properties("Season", capabilities.ALL)
    assert properties("Movie", frozenset()) == properties("Movie", capabilities.ALL)
    minimal = properties("Episode", frozenset(), minimal=True)
    assert "file" in minimal and "tag" not in minimal


def _plotted_show_with_a_favourite():
    items = show_bundle(seasons=(1,), episodes=2)
    next(item for item in items if item["Type"] == "Season")["Overview"] = "An arc"
    next(item for item in items if item["Type"] == "Episode")["UserData"] = {
        "IsFavorite": True
    }
    return items


def test_a_piers_imports_a_show_without_asking_for_what_it_lacks(store, backend, piers):
    store.publish(_plotted_show_with_a_favourite(), library=LIB)
    backend.setup()
    backend.reconcile()
    assert not store.pending()
    _pending, failed, error = store.outstanding()
    assert failed == 0 and not error
    # The fake refuses what a Piers refuses; beyond that, nothing asked for
    # a season's plot or an episode's tag, and nothing sent one.
    for method in ("VideoLibrary.GetEpisodes", "VideoLibrary.GetEpisodeDetails"):
        assert all("tag" not in wanted for wanted in _requested(piers, method))
    for method in ("VideoLibrary.GetSeasons", "VideoLibrary.GetSeasonDetails"):
        assert all("plot" not in wanted for wanted in _requested(piers, method))
    assert all(
        "plot" not in sent for sent in methods(piers, "VideoLibrary.SetSeasonDetails")
    )
    assert all(
        "tag" not in sent for sent in methods(piers, "VideoLibrary.SetEpisodeDetails")
    )
    assert piers.rows["Season"] and all(
        not row["plot"] for row in piers.rows["Season"].values()
    )
    assert piers.rows["Episode"] and all(
        "tag" not in row for row in piers.rows["Episode"].values()
    )


def test_the_same_profile_on_a_kodi_23_patches_what_piers_could_not(
    store, backend, piers, monkeypatch
):
    """The feature set is an input of every acknowledgement: a Kodi upgrade
    compares the rows again, and the plot and the tag land."""
    store.publish(_plotted_show_with_a_favourite(), library=LIB)
    backend.setup()
    backend.reconcile()
    assert not store.pending()
    monkeypatch.setattr(capabilities, "features", lambda: capabilities.ALL)
    piers.calls.clear()
    backend.reconcile()
    assert not store.pending()
    assert any(
        sent.get("plot") == "An arc"
        for sent in methods(piers, "VideoLibrary.SetSeasonDetails")
    )
    assert any(
        "Favorite episodes" in (sent.get("tag") or [])
        for sent in methods(piers, "VideoLibrary.SetEpisodeDetails")
    )
    assert [row["plot"] for row in piers.rows["Season"].values()] == ["An arc"]
    tagged = [
        row
        for row in piers.rows["Episode"].values()
        if "Favorite episodes" in row["tag"]
    ]
    assert len(tagged) == 1
