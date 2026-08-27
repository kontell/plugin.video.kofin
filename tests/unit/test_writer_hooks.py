"""Writer hooks (docs/sync-refactor-phase1-plan.md P1.5): the pipeline's
additions to a write live outside the writers and are registered on them."""

from kofin.sync.hooks import WriterHooks, pipeline_hooks


def test_empty_hooks_add_nothing():
    hooks = WriterHooks()
    assert hooks.extra_tags("movie", object(), {}) == []
    hooks.after_write("movie", object(), {})  # no-op, no error
    assert hooks.kinds() == {"tags": [], "post_write": []}


def test_hooks_dispatch_by_kind_and_in_order():
    seen = []
    hooks = WriterHooks(
        tags={"movie": [lambda w, o: ["a"], lambda w, o: None, lambda w, o: ("b",)]},
        post_write={
            "episode": [
                lambda w, o: seen.append(("1", o["Id"])),
                lambda w, o: seen.append(("2", o["Id"])),
            ]
        },
    )

    assert hooks.extra_tags("movie", "writer", {}) == ["a", "b"]
    assert hooks.extra_tags("tvshow", "writer", {}) == []
    hooks.after_write("episode", "writer", {"Id": "e1"})
    hooks.after_write("movie", "writer", {"Id": "m1"})
    assert seen == [("1", "e1"), ("2", "e1")]


def test_pipeline_composition_covers_the_moved_sites():
    """The four writer sites the shell used to reach into: the movie and
    show tags, the movie/episode/song repoints, the album/song source link."""
    assert pipeline_hooks().kinds() == {
        "tags": ["movie", "tvshow"],
        "post_write": ["album", "episode", "movie", "song"],
    }


def test_writers_no_longer_import_the_shell():
    import ast
    import pathlib

    for name in ("movies", "tvshows", "musicvideos", "music"):
        tree = ast.parse(
            pathlib.Path("lib/kofin/sync/writers/%s.py" % name).read_text()
        )
        modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        }
        assert not any(m.startswith("kofin.downloads") for m in modules), name
        assert "kofin.sync.musicsources" not in modules, name
