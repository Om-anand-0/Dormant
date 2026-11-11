"""Scan, then add last activity and in-use state: what ``scan`` and ``hibernate`` share."""

import time
from collections.abc import Collection, Sequence
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from dormant import locks, scanner, staleness
from dormant.detectors import Detector, load_detectors


@dataclass(frozen=True, slots=True)
class Row:
    """A project with its wanted targets, last activity and whether it is in use."""

    project: scanner.Project
    last_activity: float
    in_use: bool

    @property
    def reclaimable(self) -> int:
        """Reclaimable bytes over the targets Dormant may touch (not git-tracked)."""
        return sum(t.reclaimable_bytes for t in self.project.targets if not t.tracked)


def survey(
    path: Path,
    kinds: Collection[str],
    max_age: timedelta | None,
    ignore: Sequence[str] = (),
    detectors: Sequence[Detector] | None = None,
) -> list[Row]:
    """Scan ``path``, keep the wanted kinds, add activity and in-use, apply ``max_age``.

    Args:
        path: Folder to scan.
        kinds: Target kinds to keep; projects left with none are dropped.
        max_age: Keep only projects idle at least this long (None keeps all).
        ignore: Glob patterns passed to the scanner.
        detectors: Detectors to use; the bundled registry when None.

    Returns:
        Rows sorted by reclaimable bytes, largest first.
    """
    dets = load_detectors() if detectors is None else detectors
    projects = list(scanner.scan(path, dets, ignore))
    all_targets = [t.path for p in projects for t in p.targets]
    snap = locks.snapshot()
    cutoff = time.time() - max_age.total_seconds() if max_age else None
    rows = []
    for p in projects:
        targets = tuple(t for t in p.targets if t.kind in kinds)
        if not targets:
            continue
        inside = [t for t in all_targets if t.is_relative_to(p.root)]
        active = staleness.last_activity(p.root, inside)
        if cutoff is not None and active >= cutoff:
            continue
        rows.append(Row(replace(p, targets=targets), active, locks.in_use(p.root, snap)))
    return sorted(rows, key=lambda r: r.reclaimable, reverse=True)
