"""``ReleaseBridgeBuilder`` — from the linking release to the background release.

Agribalyse names its ecoinvent inputs in the release it was built on (the
linking release). Scoring it on a later release needs, for every dataset
and elementary flow of the former, what stands for it in the latter:

* datasets — ecoinvent publishes one correspondence workbook per release
  step. Each row pairs a dataset of the old release with a dataset of the
  new one, both by their published ids, and gives a *replacement amount*:
  the share of the old dataset the new one takes over (1 unless the dataset
  was split, e.g. a ``RoW`` into new countries). ``CorrespondenceChain``
  orders the workbooks from one release to the other and multiplies the
  shares along the way.
* elementary flows — ecoinvent keeps a flow's id across releases, also when
  it renames it. ``FlowBridge`` pairs the two snapshots by id, then by
  ``(name, categories, unit)`` for the few ids that went away.

The workbooks are licensed ecoinvent material, and so is what is built from
them: both tables land in ``registry/``, which never ships.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import pandas as pd

from config import EcoinventRelease, Settings
from core.logging import Logging
from core.parquet_io import ParquetAtomicWriter


class ReleaseBridgeError(RuntimeError):
    """The bridge cannot be built from what is on disk."""


@dataclass(frozen=True)
class CorrespondenceStep:
    """One workbook: the datasets of ``old_version`` and what replaces them in ``new_version``."""

    old_version: str
    new_version: str
    rows: pd.DataFrame
    """``old_activity_uuid, old_product_uuid, old_label, new_activity_uuid,
    new_product_uuid, new_label, share``. A dataset removed without a
    replacement has its ``new_*`` columns empty. Labels are
    ``activity name | geography | product name``."""


@dataclass(frozen=True)
class CorrespondenceFileReader:
    """Read the sheet of one system model out of a correspondence workbook."""

    system_model: str = "cutoff"

    SHEETS: ClassVar[dict[str, str]] = {
        "cutoff": "Cut-off",
        "apos": "APOS",
        "consequential": "Consequential",
        "EN15804": "EN15804",
    }
    VERSIONS: ClassVar[re.Pattern[str]] = re.compile(
        r"v\.?\s*(\d+(?:\.\d+)*)\s*-\s*v\.?\s*(\d+(?:\.\d+)*)", re.IGNORECASE
    )
    # Columns of each side, in the order the workbook lists them.
    SIDE_WIDTH: ClassVar[int] = 13
    ACTIVITY_UUID: ClassVar[int] = 0
    ACTIVITY_NAME: ClassVar[int] = 3
    GEOGRAPHY: ClassVar[int] = 4
    PRODUCT_UUID: ClassVar[int] = 8
    PRODUCT_NAME: ClassVar[int] = 9
    SHARE_HEADER: ClassVar[str] = "replacement amount"

    @classmethod
    def versions_of(cls, path: Path) -> tuple[str, str] | None:
        """``(old, new)`` release versions a workbook's file name states, if any."""
        match = cls.VERSIONS.search(path.name)
        return (match.group(1), match.group(2)) if match else None

    def read(self, path: Path) -> CorrespondenceStep:
        from openpyxl import load_workbook

        versions = self.versions_of(path)
        if versions is None:
            raise ReleaseBridgeError(f"{path.name}: no 'vX - vY' release pair in the file name.")
        workbook = load_workbook(path, read_only=True, data_only=True)
        sheet_name = self.SHEETS[self.system_model]
        if sheet_name not in workbook.sheetnames:
            raise ReleaseBridgeError(f"{path.name}: no sheet {sheet_name!r}.")
        rows = workbook[sheet_name].iter_rows(values_only=True)
        next(rows)  # release versions, one per side
        header = [str(cell or "").strip().lower() for cell in next(rows)]
        if self.SHARE_HEADER not in header:
            raise ReleaseBridgeError(f"{path.name}: no {self.SHARE_HEADER!r} column.")
        share_at = header.index(self.SHARE_HEADER)
        records = [record for row in rows if (record := self._record(row, share_at)) is not None]
        return CorrespondenceStep(
            old_version=versions[0],
            new_version=versions[1],
            rows=pd.DataFrame.from_records(records, columns=self._columns()),
        )

    @staticmethod
    def _columns() -> list[str]:
        return [
            "old_activity_uuid",
            "old_product_uuid",
            "old_label",
            "new_activity_uuid",
            "new_product_uuid",
            "new_label",
            "share",
        ]

    def _record(self, row: tuple[Any, ...], share_at: int) -> tuple[Any, ...] | None:
        old = self._side(row, 0)
        if old is None:  # a dataset the new release adds: nothing to bridge
            return None
        new = self._side(row, self.SIDE_WIDTH) or (None, None, None)
        share = row[share_at] if len(row) > share_at else None
        return (*old, *new, float(share) if new[0] and share is not None else 0.0)

    def _side(self, row: tuple[Any, ...], offset: int) -> tuple[str, str, str] | None:
        if len(row) <= offset + self.PRODUCT_NAME or not row[offset + self.ACTIVITY_UUID]:
            return None
        label = " | ".join(
            str(row[offset + column] or "")
            for column in (self.ACTIVITY_NAME, self.GEOGRAPHY, self.PRODUCT_NAME)
        )
        return (
            str(row[offset + self.ACTIVITY_UUID]),
            str(row[offset + self.PRODUCT_UUID]),
            label,
        )


@dataclass(frozen=True)
class CorrespondenceChain:
    """The workbooks of a directory, ordered from one release to another."""

    steps: tuple[CorrespondenceStep, ...]

    @classmethod
    def discover(
        cls, directory: Path, start: str, end: str, reader: CorrespondenceFileReader
    ) -> CorrespondenceChain:
        """Order the workbooks that lead from ``start`` to ``end``.

        ecoinvent publishes no workbook for a patch release (3.10 to 3.10.1):
        a step starting at a patch of the release reached is taken as its
        continuation, and ``compose`` joins the two on the published ids.
        """
        by_old: dict[str, Path] = {}
        for path in sorted(directory.glob("*.xlsx")):
            versions = reader.versions_of(path)
            if versions is not None and not path.name.startswith("~"):
                by_old[versions[0]] = path
        steps: list[CorrespondenceStep] = []
        reached = start
        while reached != end:
            path = by_old.get(reached) or next(
                (p for old, p in sorted(by_old.items()) if old.startswith(f"{reached}.")), None
            )
            if path is None:
                raise ReleaseBridgeError(
                    f"No correspondence workbook starts at ecoinvent {reached} in {directory} "
                    f"(looking for the chain {start} -> {end}). Download it from ecoQuery "
                    f"(Files > Supporting documents)."
                )
            step = reader.read(path)
            steps.append(step)
            if step.new_version == reached:
                raise ReleaseBridgeError(f"{path.name} does not move past ecoinvent {reached}.")
            reached = step.new_version
        return cls(steps=tuple(steps))

    def compose(self) -> tuple[pd.DataFrame, list[dict[str, Any]]]:
        """``old_activity_uuid, old_product_uuid, old_label, new_activity_uuid,
        new_product_uuid, new_label, share`` from the first release to the last,
        and one line of figures per step. A dataset removed on the way, or the
        removed part of a split one, simply has no row: its shares sum below 1."""
        first = self.steps[0].rows
        current = first[first["new_activity_uuid"].notna()].copy()
        stats = [self._step_stats(self.steps[0], joined_by_label=0)]
        for step in self.steps[1:]:
            joined = self._join(current, step.rows)
            kept = joined[joined["next_activity_uuid"].notna()].copy()
            kept["share"] = kept["share"] * kept["step_share"]
            current = kept[
                [*self._OLD, "next_activity_uuid", "next_product_uuid", "next_label", "share"]
            ]
            current = current.rename(
                columns={
                    "next_activity_uuid": "new_activity_uuid",
                    "next_product_uuid": "new_product_uuid",
                    "next_label": "new_label",
                }
            )
            stats.append(self._step_stats(step, joined_by_label=int(joined["by_label"].sum())))
        keys = ["old_activity_uuid", "old_product_uuid", "new_activity_uuid", "new_product_uuid"]
        bridge = current.groupby(keys, as_index=False, sort=True).agg(
            old_label=("old_label", "first"),
            new_label=("new_label", "first"),
            share=("share", "sum"),
        )
        return bridge[bridge["share"] != 0.0].reset_index(drop=True), stats

    _OLD: ClassVar[tuple[str, ...]] = ("old_activity_uuid", "old_product_uuid", "old_label")

    @staticmethod
    def _join(current: pd.DataFrame, step_rows: pd.DataFrame) -> pd.DataFrame:
        """Join on the published ids; on the label where a release reissued them."""
        nxt = step_rows.rename(
            columns={
                "old_activity_uuid": "mid_activity_uuid",
                "old_product_uuid": "mid_product_uuid",
                "old_label": "mid_label",
                "share": "step_share",
                "new_activity_uuid": "next_activity_uuid",
                "new_product_uuid": "next_product_uuid",
                "new_label": "next_label",
            }
        )
        by_id = current.merge(
            nxt,
            how="left",
            left_on=["new_activity_uuid", "new_product_uuid"],
            right_on=["mid_activity_uuid", "mid_product_uuid"],
        )
        by_id["by_label"] = False
        missing = by_id["step_share"].isna()
        if not missing.any():
            return by_id
        retry = by_id.loc[missing, list(current.columns)].merge(
            nxt, how="left", left_on="new_label", right_on="mid_label"
        )
        retry["by_label"] = retry["step_share"].notna()
        return pd.concat([by_id[~missing], retry], ignore_index=True)

    @staticmethod
    def _step_stats(step: CorrespondenceStep, *, joined_by_label: int) -> dict[str, Any]:
        rows = step.rows
        replaced = rows[rows["new_activity_uuid"].notna()]
        fan_out = replaced.groupby(["old_activity_uuid", "old_product_uuid"]).size()
        return {
            "from": step.old_version,
            "to": step.new_version,
            "datasets": len(rows.drop_duplicates(["old_activity_uuid", "old_product_uuid"])),
            "split": int((fan_out > 1).sum()),
            "removed": int(rows["new_activity_uuid"].isna().sum()),
            "joined_by_label": joined_by_label,
        }


@dataclass(frozen=True)
class FlowBridge:
    """Pair the elementary flows of two releases: by id, then by name."""

    @staticmethod
    def build(old_flows: list[dict[str, Any]], new_flows: list[dict[str, Any]]) -> pd.DataFrame:
        """``old_code, new_code, how``. ``how`` is ``id`` (same id, same unit),
        ``name`` (the id went away, one flow of the new release has the same
        name, compartments and unit), ``unit`` (same id but another unit: the
        amounts cannot be carried over, no pair) or ``none``."""
        new_by_code = {flow["code"]: flow for flow in new_flows}
        old_codes = {flow["code"] for flow in old_flows}
        by_label: dict[tuple[str, tuple[str, ...], str], list[str]] = {}
        for flow in new_flows:
            by_label.setdefault(FlowBridge._label(flow), []).append(flow["code"])
        records: list[tuple[str, str | None, str]] = []
        for flow in old_flows:
            code = flow["code"]
            same_id = new_by_code.get(code)
            if same_id is not None:
                same_unit = FlowBridge._unit(same_id) == FlowBridge._unit(flow)
                records.append((code, code, "id") if same_unit else (code, None, "unit"))
                continue
            candidates = by_label.get(FlowBridge._label(flow), [])
            if len(candidates) == 1 and candidates[0] not in old_codes:
                records.append((code, candidates[0], "name"))
            else:
                records.append((code, None, "none"))
        return pd.DataFrame.from_records(records, columns=["old_code", "new_code", "how"])

    @staticmethod
    def _unit(flow: dict[str, Any]) -> str:
        return str(flow.get("unit") or "").strip().lower()

    @staticmethod
    def _label(flow: dict[str, Any]) -> tuple[str, tuple[str, ...], str]:
        return (
            str(flow.get("name") or "").strip().lower(),
            tuple(str(c).strip().lower() for c in flow.get("categories") or ()),
            FlowBridge._unit(flow),
        )


@dataclass(frozen=True)
class ReleaseBridgeBuilder:
    """Write the two bridge tables for ``settings``' linking and background releases."""

    settings: Settings
    correspondence_dir: Path

    SHARE_TOLERANCE: ClassVar[float] = 1e-6

    @property
    def _log(self) -> Any:
        return Logging.get(__name__)

    def build(self) -> dict[str, Any]:
        s = self.settings
        if not s.has_background_release:
            raise ReleaseBridgeError(
                f"No background release is set: export {s.BACKGROUND_ENV}=<version> "
                f"(or put it in .env) to bridge ecoinvent {s.linking_release.version} to it."
            )
        old, new = s.linking_release, s.background_release
        chain = CorrespondenceChain.discover(
            self.correspondence_dir,
            old.version,
            new.version,
            CorrespondenceFileReader(system_model=old.system_model),
        )
        composed, steps = chain.compose()
        activities = self._activity_bridge(composed, old, new)
        flows = FlowBridge.build(
            self._snapshot(s.paths.release_biosphere_flows_json(old))["flows"],
            self._snapshot(s.paths.release_biosphere_flows_json(new))["flows"],
        )
        s.paths.ensure_runtime_dirs()
        ParquetAtomicWriter.write(activities, s.paths.registry_release_bridge_activities)
        ParquetAtomicWriter.write(flows, s.paths.registry_release_bridge_flows)
        report = self._report(activities, flows, steps, old, new)
        self._log.info("release_bridge.built", **{k: v for k, v in report.items() if k != "steps"})
        return report

    def _activity_bridge(
        self, composed: pd.DataFrame, old: EcoinventRelease, new: EcoinventRelease
    ) -> pd.DataFrame:
        """Key the composed chain by the Brightway codes of the two snapshots."""
        old_codes = self._codes(old)
        table = composed.copy()
        table["old_code"] = self._codes_for(table, "old", old_codes)
        table["new_code"] = self._codes_for(table, "new", self._codes(new))
        table = table[table["old_code"].notna()].copy()
        table.loc[table["new_code"].isna(), "share"] = 0.0
        absent = old_codes[~old_codes["code"].isin(table["old_code"])]
        table = pd.concat(
            [
                table,
                pd.DataFrame(
                    {"old_code": absent["code"], "share": 0.0, "old_label": absent["label"]}
                ),
            ],
            ignore_index=True,
        )
        columns = ["old_code", "new_code", "share", "old_label", "new_label"]
        table = table[columns].astype({"new_code": "object", "new_label": "object"})
        return table.sort_values(["old_code", "new_code"]).reset_index(drop=True)

    @staticmethod
    def _codes_for(table: pd.DataFrame, side: str, codes: pd.DataFrame) -> pd.Series:
        """Brightway code of each row's ``side`` dataset: by its published ids, and
        by its label where the release was reissued under new ids (3.10.1)."""
        by_ids = dict(
            zip(
                zip(codes["activity_uuid"], codes["product_uuid"], strict=True),
                codes["code"],
                strict=True,
            )
        )
        unique = codes.drop_duplicates("label", keep=False)
        by_label = dict(zip(unique["label"], unique["code"], strict=True))
        ids = zip(table[f"{side}_activity_uuid"], table[f"{side}_product_uuid"], strict=True)
        labels = table[f"{side}_label"].fillna("").str.lower()
        return pd.Series(
            [
                by_ids.get(key) or by_label.get(label)
                for key, label in zip(ids, labels, strict=True)
            ],
            index=table.index,
            dtype="object",
        )

    def _codes(self, release: EcoinventRelease) -> pd.DataFrame:
        """``code, activity_uuid, product_uuid, label`` of a release's snapshot."""
        path = self.settings.paths.release_activities_json(release)
        rows = self._snapshot(path)["activities"]
        if rows and not rows[0].get("activity_uuid"):
            raise ReleaseBridgeError(
                f"{path.name} holds no published ids: rewrite it with "
                f"`dds-bootstrap-ecoinvent --ecoinvent-version {release.version}`."
            )
        return pd.DataFrame(
            [
                (
                    r["code"],
                    r["activity_uuid"],
                    r["product_uuid"],
                    " | ".join(
                        str(r.get(k) or "") for k in ("name", "location", "reference_product")
                    ).lower(),
                )
                for r in rows
            ],
            columns=["code", "activity_uuid", "product_uuid", "label"],
        )

    @staticmethod
    def _snapshot(path: Path) -> dict[str, Any]:
        if not path.exists():
            raise ReleaseBridgeError(
                f"{path} is missing: run `dds-bootstrap-ecoinvent` for both releases first."
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def _report(
        self,
        activities: pd.DataFrame,
        flows: pd.DataFrame,
        steps: list[dict[str, Any]],
        old: EcoinventRelease,
        new: EcoinventRelease,
    ) -> dict[str, Any]:
        shares = activities.groupby("old_code")["share"].sum()
        successors = activities[activities["new_code"].notna()].groupby("old_code").size()
        return {
            "from": old.version,
            "to": new.version,
            "steps": steps,
            "datasets": len(shares),
            "datasets_kept_whole": int(((shares - 1.0).abs() <= self.SHARE_TOLERANCE).sum()),
            "datasets_partly_lost": int(
                ((shares > self.SHARE_TOLERANCE) & (shares < 1.0 - self.SHARE_TOLERANCE)).sum()
            ),
            "datasets_lost": int((shares <= self.SHARE_TOLERANCE).sum()),
            "datasets_split": int((successors > 1).sum()),
            "flows": len(flows),
            "flows_by_id": int((flows["how"] == "id").sum()),
            "flows_by_name": int((flows["how"] == "name").sum()),
            "flows_unit_changed": int((flows["how"] == "unit").sum()),
            "flows_lost": int((flows["how"] == "none").sum()),
        }
