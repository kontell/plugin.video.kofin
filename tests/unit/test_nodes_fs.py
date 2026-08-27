"""The one deletion primitive under the generated trees (P2.1, nodes/fs.py):
gated on the ``kofin`` prefix everywhere, so what the user put beside our
files survives every reconcile and every teardown."""

import os

from kofin.sync.nodes import fs


def plant(root, *names):
    for name in names:
        path = os.path.join(root, name)
        if name.endswith("/"):
            os.makedirs(path, exist_ok=True)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as handle:
                handle.write(name)


def listing(root):
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            found.append(os.path.relpath(os.path.join(dirpath, name), root))
    return sorted(found)


def test_the_gate_is_the_prefix():
    assert fs.is_managed("kofinmovieslib1")
    assert fs.is_managed("kofin_Favoritemovies.xml")
    assert not fs.is_managed("Kofin")  # the playlist folder's own name
    assert not fs.is_managed("index.xml")
    assert not fs.is_managed("mine.xml")


def test_reconcile_removes_only_managed_entries_not_kept(tmp_path):
    root = str(tmp_path)
    plant(
        root,
        "kofinmovieslib1/all.xml",
        "kofinmovieslib2/all.xml",
        "kofin_Favoritemovies.xml",
        "kofin_DownloadedMovies.xml",
        "index.xml",
        "mine.xml",
        "mine/all.xml",
    )

    removed = fs.remove_managed_entries(
        root, keep=("kofinmovieslib1", "kofin_Favoritemovies.xml")
    )

    assert sorted(removed) == ["kofin_DownloadedMovies.xml", "kofinmovieslib2"]
    assert listing(root) == [
        "index.xml",
        "kofin_Favoritemovies.xml",
        "kofinmovieslib1",
        "kofinmovieslib1/all.xml",
        "mine",
        "mine.xml",
        "mine/all.xml",
    ]


def test_teardown_takes_the_named_prefixless_files_and_spares_the_rest(tmp_path):
    root = str(tmp_path / "Kofin")
    plant(root, "kofinmovieslib1.xsp", "folder.jpg", "mine.xsp")

    fs.remove_managed_entries(root, also=("folder.jpg",), label="playlist")
    assert listing(root) == ["mine.xsp"]

    # The folder stays while something foreign lives in it...
    assert fs.remove_empty(root) is False
    assert os.path.isdir(root)

    # ...and goes once it is empty.
    os.remove(os.path.join(root, "mine.xsp"))
    assert fs.remove_empty(root) is True
    assert not os.path.exists(root)


def test_a_foreign_entry_inside_a_managed_folder_keeps_it(tmp_path):
    root = str(tmp_path)
    plant(root, "kofinmovieslib1/all.xml", "kofinmovieslib1/theirs/note.txt")

    fs.remove_managed_entries(root)

    assert listing(root) == [
        "kofinmovieslib1",
        "kofinmovieslib1/theirs",
        "kofinmovieslib1/theirs/note.txt",
    ]


def test_listdir_of_a_missing_directory_is_empty(tmp_path):
    assert fs.listdir(str(tmp_path / "nope")) == ([], [])
    assert fs.remove_empty(str(tmp_path / "nope")) is False
    assert fs.remove_managed_entries(str(tmp_path / "nope")) == []


def test_delete_file_tolerates_a_missing_file(tmp_path):
    fs.delete_file(str(tmp_path / "gone.xml"))  # no raise
