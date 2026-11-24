"""Every collection the code names is in the data map."""

from __future__ import annotations

import re
from pathlib import Path

from app import datamap

_APP = Path(datamap.__file__).parent
_LITERAL = re.compile(
    r"""\.collection(?:_group)?\(\s*["']([A-Za-z][A-Za-z0-9_]*)["']"""
)
# A literal also names a subcollection when chained: `.document(x).collection("book")`.


def _literals() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in sorted(_APP.rglob("*.py")):
        if path.name == "sqlitestore.py":
            continue  # the shim defines the API; its examples are docstrings
        for m in _LITERAL.finditer(path.read_text()):
            found.setdefault(m.group(1), str(path.relative_to(_APP.parent)))
    return found


def test_every_collection_literal_is_registered() -> None:
    missing = {n: f for n, f in _literals().items() if n not in datamap.BY_NAME}
    assert not missing, (
        f"collections without a datamap row (retention + export decision): {missing}"
    )


def test_registry_has_no_unused_rows() -> None:
    # A row for a collection no code uses would hide a rename.
    unused = sorted(set(datamap.BY_NAME) - set(_literals()))
    assert not unused, f"datamap rows with no collection(...) literal in app/: {unused}"


def test_registry_parents_exist_and_device_and_bridge_rows_name_their_owner() -> None:
    """`app/purge.py` cascades a device or bridge delete through `links`."""
    for c in datamap.REGISTRY:
        if c.parent is not None:
            assert c.parent in datamap.BY_NAME, c
            continue
        if c.family == "via:device":
            assert any(link.startswith("device:") for link in c.links), c.name
        if c.family == "via:bridge":
            assert any(link.startswith("bridge:") for link in c.links), c.name
        for link in c.links:
            kind, _, where = link.partition(":")
            assert kind in ("device", "bridge") and where, (c.name, link)
