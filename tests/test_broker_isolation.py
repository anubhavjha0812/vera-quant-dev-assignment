"""Enforces CLAUDE.md rule 5 / DECISIONS.md #3: `brokers/paper` and
`brokers/live_smartapi` must never import each other, and `brokers/base.py`
must never import either concrete broker. A source-level check (not just
"it happens to work today") so this can never silently regress once
`brokers/live_smartapi` exists (step 12).
"""
from pathlib import Path

BROKERS_DIR = Path(__file__).parent.parent / "src" / "vera_quant" / "brokers"


def _source_files(subpath: str) -> list[Path]:
    target = BROKERS_DIR / subpath
    if target.is_file():
        return [target]
    if not target.exists():
        return []
    return list(target.rglob("*.py"))


def _import_lines(path: Path) -> list[str]:
    """Only actual `import`/`from ... import` lines — not docstring
    mentions of the forbidden name (which this module's own docstring
    has, by design, explaining why it doesn't import it)."""
    return [
        line
        for line in path.read_text().splitlines()
        if line.strip().startswith(("import ", "from "))
    ]


def test_paper_package_never_imports_live_smartapi() -> None:
    for path in _source_files("paper"):
        for line in _import_lines(path):
            assert "live_smartapi" not in line, f"{path}: {line!r} breaks package isolation"


def test_base_interface_never_imports_a_concrete_broker() -> None:
    for path in _source_files("base.py"):
        for line in _import_lines(path):
            assert "brokers.paper" not in line
            assert "brokers.live_smartapi" not in line


def test_paper_broker_package_works_with_live_smartapi_absent() -> None:
    """live_smartapi doesn't exist yet (step 12) — this is the actual
    "delete the other package and nothing breaks" check for now; once
    step 12 adds it, the equivalent test lands there for the reverse case.
    """
    assert not (BROKERS_DIR / "live_smartapi").exists()
    from vera_quant.brokers.paper import PaperBroker  # noqa: F401 -- import succeeding IS the test
