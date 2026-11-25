"""``BackgroundRebaser`` — move the linked inventory onto the background release.

Runs after ``TechnosphereMatcher``. Every Agribalyse input that was linked
to a dataset of the linking release is moved to what stands for that
dataset in the background release (``ReleaseBridge``, built by
``dds-build-release-bridge``):

* one successor taking the whole dataset: the input is relinked as it is;
* several successors (a dataset ecoinvent split): the input is repeated
  once per successor, its amount times the successor's share;
* shares summing below one (a part of the dataset lost its successor at a
  later release step): the input still needs its whole amount, so the
  remaining successors take it in proportion to their shares, and the
  dataset is counted and listed;
* no successor at all: the input is unlinked, counted and listed, never
  silently kept on the old release.

Elementary flows stay in the linking release's namespace: the background
exchanges are brought into it when they are read (``ReleaseBridge.
background_flow_renames``), so that the CF registry, the regional
corrections and every rule keyed on that namespace keep applying.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, ClassVar

import pandas as pd

from config import Settings
from core.logging import Logging


class ReleaseBridgeMissingError(FileNotFoundError):
    """A background release is set but its bridge tables are not built."""


@dataclass(frozen=True)
class ReleaseBridge:
    """The bridge tables of ``dds-build-release-bridge``, indexed for lookups."""

    linking_db: str
    background_db: str
    linking_biosphere_db: str
    background_biosphere_db: str
    successors: Mapping[str, tuple[tuple[str, float], ...]]
    """Linking-release activity code -> ``((background code, share), ...)``."""
    flows: Mapping[str, str]
    """Linking-release elementary-flow code -> background-release code."""

    @classmethod
    def load(cls, settings: Settings) -> ReleaseBridge:
        paths = settings.paths
        for path in (paths.registry_release_bridge_activities, paths.registry_release_bridge_flows):
            if not path.exists():
                raise ReleaseBridgeMissingError(
                    f"{path} is missing. With {settings.BACKGROUND_ENV} set, run "
                    f"`dds-build-release-bridge --correspondence-dir <dir>` first."
                )
        activities = pd.read_parquet(paths.registry_release_bridge_activities)
        flows = pd.read_parquet(paths.registry_release_bridge_flows)
        return cls.from_frames(settings, activities, flows)

    @classmethod
    def from_frames(
        cls, settings: Settings, activities: pd.DataFrame, flows: pd.DataFrame
    ) -> ReleaseBridge:
        successors: dict[str, list[tuple[str, float]]] = {}
        kept = activities[activities["new_code"].notna() & (activities["share"] != 0.0)]
        for old_code, new_code, share in zip(
            kept["old_code"], kept["new_code"], kept["share"], strict=True
        ):
            successors.setdefault(str(old_code), []).append((str(new_code), float(share)))
        linked = flows[flows["new_code"].notna()]
        return cls(
            linking_db=settings.linking_release.db_name,
            background_db=settings.background_release.db_name,
            linking_biosphere_db=settings.biosphere_db_name,
            background_biosphere_db=settings.background_release.biosphere_db_name,
            successors={code: tuple(rows) for code, rows in successors.items()},
            flows=dict(
                zip(linked["old_code"].astype(str), linked["new_code"].astype(str), strict=True)
            ),
        )

    def background_flow_renames(self) -> dict[tuple[str, str], tuple[str, str]]:
        """Background elementary flow -> the linking-release flow it continues.

        A background flow no linking-release flow leads to is not listed: it
        keeps its own ``(database, code)`` and is characterised by the
        background release's factors (``BackgroundCfSupplement``).
        """
        return {
            (self.background_biosphere_db, new_code): (self.linking_biosphere_db, old_code)
            for old_code, new_code in self.flows.items()
        }


@dataclass
class BackgroundRebaser:
    """Relink the exchanges of ``sp.data`` from the linking to the background release."""

    settings: Settings
    bridge: ReleaseBridge
    lost: Counter[str] = field(default_factory=Counter)

    WHOLE: ClassVar[float] = 1e-9
    SCALED_FIELDS: ClassVar[tuple[str, ...]] = ("amount", "loc", "minimum", "maximum")
    LOST_SAMPLE: ClassVar[int] = 50

    @property
    def _log(self) -> Any:
        return Logging.get(__name__)

    def apply(self, sp: Any) -> dict[str, Any]:
        stats: Counter[str] = Counter()
        for ds in sp.data:
            exchanges = ds.get("exchanges")
            if not exchanges:
                continue
            rebased: list[dict] = []
            for exc in exchanges:
                rebased.extend(self._rebase(exc, stats))
            ds["exchanges"] = rebased
        report = {
            **{key: int(value) for key, value in sorted(stats.items())},
            "datasets_not_whole": len(self.lost),
            "not_whole_sample": [
                {"code": code, "exchanges": n}
                for code, n in self.lost.most_common(self.LOST_SAMPLE)
            ],
        }
        self._log.info(
            "background.rebased", **{k: v for k, v in report.items() if k != "not_whole_sample"}
        )
        return report

    def _rebase(self, exc: dict, stats: Counter[str]) -> list[dict]:
        key = exc.get("input")
        if not key or key[0] != self.bridge.linking_db:
            return [exc]
        successors = self.bridge.successors.get(str(key[1]), ())
        kept = sum(share for _, share in successors)
        if exc.get("type") == "production":
            return [self._rebase_production(exc, successors, stats)]
        if not successors:
            stats["exchanges_lost"] += 1
            self.lost[str(key[1])] += 1
            return [self._unlinked(exc)]
        if abs(kept - 1.0) > self.WHOLE:
            stats["exchanges_renormalised"] += 1
            self.lost[str(key[1])] += 1
        stats["exchanges_relinked" if len(successors) == 1 else "exchanges_split"] += 1
        return [self._moved(exc, new_code, share / kept) for new_code, share in successors]

    def _rebase_production(
        self, exc: dict, successors: tuple[tuple[str, float], ...], stats: Counter[str]
    ) -> dict:
        """A process cannot produce a share of several datasets: its product is
        moved only when the dataset has a single successor."""
        if len(successors) == 1:
            stats["productions_relinked"] += 1
            return self._moved(exc, successors[0][0], 1.0)
        stats["productions_lost"] += 1
        return self._unlinked(exc)

    def _moved(self, exc: dict, new_code: str, share: float) -> dict:
        moved = dict(exc)
        moved["input"] = (self.bridge.background_db, new_code)
        if share != 1.0:
            for name in self.SCALED_FIELDS:
                if isinstance(moved.get(name), (int, float)):
                    moved[name] = moved[name] * share
        return moved

    @staticmethod
    def _unlinked(exc: dict) -> dict:
        unlinked = dict(exc)
        unlinked.pop("input", None)
        return unlinked
