import os
from pathlib import Path

import pytest
from fixtures.trees import ECOSYSTEMS, snapshot

from dormant.archiver import hibernate
from dormant.index import ArchiveState, Index
from dormant.restorer import WakeResult, wake

CASES = [
    pytest.param(eco, name, kind, id=f"{eco}-{name}")
    for eco, (_, _, kinds) in ECOSYSTEMS.items()
    for name, kind in kinds.items()
]


@pytest.mark.parametrize(("eco", "name", "kind"), CASES)
def test_round_trip(tmp_path: Path, index: Index, eco: str, name: str, kind: str) -> None:
    ECOSYSTEMS[eco][1](tmp_path)
    target = tmp_path / name
    before = snapshot(target)
    rest = {p.name: snapshot(p) if p.is_dir() else p.read_bytes() for p in tmp_path.iterdir()}
    del rest[name]

    rec = hibernate(target, tmp_path, kind, index=index)
    assert not os.path.lexists(target)
    assert rec.archive_path is not None
    assert rec.archive_path.is_file()
    assert index.get(rec.id).state == ArchiveState.ACTIVE  # type: ignore[union-attr]

    assert wake(target, index=index) == WakeResult.RESTORED
    assert snapshot(target) == before
    assert not rec.archive_path.exists()
    assert index.get(rec.id).state == ArchiveState.RESTORED  # type: ignore[union-attr]
    after = {p.name: snapshot(p) if p.is_dir() else p.read_bytes() for p in tmp_path.iterdir()}
    assert after == {**rest, name: after[name]}  # siblings (other target, markers) untouched
