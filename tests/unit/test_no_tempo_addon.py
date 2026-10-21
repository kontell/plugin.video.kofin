"""The Kodi 23 build has no inputstream.tempo route (docs/syncplay-fine-sync.md).

Fine sync on master plays through Kodi's own tempo. The add-on route lives on
the api and sql branches, which share code with this one by cherry-pick, and
inputstream.tempo has no Kodi 23 build: a port that brought the route back
would hand Kodi an inputstream it does not have on every group play. Comments
and docstrings may name the add-on; code may not.
"""

import ast
from pathlib import Path

LIB = Path(__file__).resolve().parents[2] / "lib"


def _docstrings(tree):
    """The string constants that are docstrings, by identity."""
    found = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                found.add(id(body[0].value))
    return found


def test_no_code_names_the_tempo_addon():
    offenders = []
    for path in sorted(LIB.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstrings(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "inputstream.tempo" in node.value
                and id(node) not in docstrings
            ):
                offenders.append("%s:%d" % (path.relative_to(LIB), node.lineno))
    assert offenders == []


def test_the_guard_sees_code():
    tree = ast.parse(
        '"""inputstream.tempo in a docstring"""\nX = "inputstream.tempo"\n'
    )
    docstrings = _docstrings(tree)
    code = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and id(node) not in docstrings
    ]
    assert [node.value for node in code] == ["inputstream.tempo"]
