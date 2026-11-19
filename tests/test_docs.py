"""The Python examples in the technical docs run as written (offline)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOCS = ("docs/SEMANTIC_LAYER.md", "docs/SQL_GUARDS.md", "docs/LIVE_DASHBOARDS.md")
PYTHON_BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)


def _blocks() -> list[tuple[str, int, str]]:
    found = []
    for doc in DOCS:
        text = (ROOT / doc).read_text(encoding="utf-8")
        for index, match in enumerate(PYTHON_BLOCK.finditer(text)):
            found.append((doc, index, match.group(1)))
    return found


BLOCKS = _blocks()


def test_every_doc_has_examples():
    assert {doc for doc, _, _ in BLOCKS} == set(DOCS)


@pytest.mark.parametrize(("doc", "index", "code"), BLOCKS, ids=[f"{d}#{i}" for d, i, _ in BLOCKS])
def test_doc_example_runs(doc, index, code, monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    monkeypatch.delenv("DB_URL", raising=False)
    monkeypatch.delenv("NEXDASH_DB_URL", raising=False)
    exec(compile(code, f"{doc}#{index}", "exec"), {"__name__": "__doc_example__"})  # noqa: S102


def test_sql_guard_example_output_matches_doc(monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    doc = (ROOT / "docs/SQL_GUARDS.md").read_text(encoding="utf-8")
    code = PYTHON_BLOCK.findall(doc)[0]
    exec(compile(code, "SQL_GUARDS.md#0", "exec"), {"__name__": "__doc_example__"})  # noqa: S102
    printed = [line.rstrip() for line in capsys.readouterr().out.strip().splitlines()]
    documented = doc.split("Output:\n\n```\n", 1)[1].split("```", 1)[0].strip().splitlines()
    assert printed == documented
