"""Enforces CLAUDE.md rule 5 / DECISIONS.md #3: `brokers/paper` and
`brokers/live_smartapi` must never import each other, and `brokers/base.py`
must never import either concrete broker. A source-level check (not just
"it happens to work today") so this can never silently regress once
`brokers/live_smartapi` exists (step 12).
"""
import ast
from pathlib import Path

BROKERS_DIR = Path(__file__).parent.parent / "src" / "vera_quant" / "brokers"


def _source_files(subpath: str) -> list[Path]:
    target = BROKERS_DIR / subpath
    if target.is_file():
        return [target]
    if not target.exists():
        return []
    return list(target.rglob("*.py"))


def _imported_modules(path: Path) -> set[str]:
    """Real imported module names, parsed via `ast` — immune to a
    docstring merely *mentioning* a forbidden name (this package's own
    docstrings do, by design, to explain why they don't import it)."""
    tree = ast.parse(path.read_text(), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_paper_package_never_imports_live_smartapi() -> None:
    for path in _source_files("paper"):
        modules = _imported_modules(path)
        assert not any("live_smartapi" in m for m in modules), f"{path}: {modules}"


def test_live_smartapi_package_never_imports_paper() -> None:
    for path in _source_files("live_smartapi"):
        modules = _imported_modules(path)
        assert not any("brokers.paper" in m for m in modules), f"{path}: {modules}"


def test_base_interface_never_imports_a_concrete_broker() -> None:
    for path in _source_files("base.py"):
        modules = _imported_modules(path)
        assert not any("brokers.paper" in m or "brokers.live_smartapi" in m for m in modules)


def test_both_broker_packages_import_cleanly_side_by_side() -> None:
    """Runtime complement to the static checks above: importing both at
    once (as the step-14 factory eventually will) never raises, which
    would be the symptom of an accidental coupling the source scan missed.
    """
    from vera_quant.brokers.live_smartapi import SmartApiBroker  # noqa: F401
    from vera_quant.brokers.paper import PaperBroker  # noqa: F401
