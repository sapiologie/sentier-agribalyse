"""``LinkedInventoryExporter`` — the linked Agribalyse inventory as one JSON file.

For a store that already holds ecoinvent (its datasets with their scores,
its elementary flows with their factors), Agribalyse reduces to what each
of its processes takes per unit of its reference product:

* other processes of the file,
* ecoinvent datasets, named by the ids ecoinvent publishes them under
  (``<activity id>_<product id>``),
* ecoinvent elementary flows, named by their id, name and compartments.

That is what this export writes, read off the matrices of the last link
run (``ScoringPackage``): after allocation of the multi-product processes,
de-duplication and pruning, so the file describes the very inventory the
backtest scores. With a background release it names that release's
datasets and flows.

Regional corrections are not part of it: they are a property of the
characterisation, not of the inventory. An elementary flow that is not the
release's own (a flow of the EF list, a flow the release dropped) is given
the release's flow of the same substance and compartment when there is one
(``BackgroundFlowResolver``); what remains cannot be named for an ecoinvent
store and is left out, counted and listed in the report.

The file embeds ecoinvent ids and amounts of the embedded ecoinvent copies:
it is licensed data and is written under ``cache/``, which never ships.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
import pandas as pd

from config import Settings
from core.logging import Logging
from exports.background_flow_resolver import BackgroundFlowResolver
from scoring.exchange_frame_builder import ExchangeFrameBuilder
from scoring.scoring_package import ScoringPackage
from transforms.background_rebase import ReleaseBridge


@dataclass(frozen=True)
class LinkedInventoryExporter:
    settings: Settings
    package: ScoringPackage

    FORMAT: ClassVar[int] = 1
    REGION_SEPARATOR: ClassVar[str] = "@"
    COPIED: ClassVar[re.Pattern[str]] = re.compile(r"copied from ecoinvent", re.IGNORECASE)
    ADAPTED: ClassVar[re.Pattern[str]] = re.compile(r"adapted from ecoinvent", re.IGNORECASE)
    UNSPECIFIED: ClassVar[str] = "unspecified"

    @property
    def _log(self) -> Any:
        return Logging.get(__name__)

    def export(self, target: Path) -> dict[str, Any]:
        s = self.settings
        columns = pd.read_parquet(s.paths.registry_activity_catalog).set_index("activity_id")
        datasets = self._dataset_identifiers()
        flow_keys = self._flow_keys()
        flows_out: dict[str, dict[str, str]] = {}
        dropped: Counter[str] = Counter()

        a = self.package.technosphere.matrix.tocsc()
        b = self.package.biosphere.matrix.tocsc()
        col_ids = self._ids_by_index(self.package.technosphere.col_id_to_idx)
        flow_ids = self._ids_by_index(self.package.biosphere.row_id_to_idx)
        own_db = s.resolved_agribalyse_db_name
        labelled = columns.reindex(col_ids)
        col_db = labelled["database"].astype("string").fillna("").tolist()
        col_code = labelled["code"].astype("string").fillna("").tolist()
        row_flow = [flow_keys.get(int(flow_id)) for flow_id in flow_ids]
        left_out: Counter[int] = Counter()

        processes: dict[str, dict[str, Any]] = {}
        for j, meta in enumerate(labelled.itertuples(index=False)):
            if col_db[j] != own_db:
                continue
            start, end = a.indptr[j], a.indptr[j + 1]
            rows, values = a.indices[start:end], a.data[start:end]
            production = float(values[rows == j].sum())
            if production == 0.0:
                dropped["processes_without_production"] += 1
                continue
            taken_processes: dict[str, float] = defaultdict(float)
            taken_datasets: dict[str, float] = defaultdict(float)
            for i, value in zip(rows.tolist(), values.tolist(), strict=True):
                if i == j:
                    continue
                amount = -value / production
                if col_db[i] == own_db:
                    taken_processes[col_code[i]] += amount
                    continue
                identifier = datasets.get((col_db[i], col_code[i]))
                if identifier is None:
                    dropped["inputs_outside_ecoinvent"] += 1
                    continue
                taken_datasets[identifier] += amount
            taken_flows: dict[str, float] = defaultdict(float)
            start, end = b.indptr[j], b.indptr[j + 1]
            for k, value in zip(
                b.indices[start:end].tolist(), b.data[start:end].tolist(), strict=True
            ):
                flow = row_flow[k]
                if flow is None:
                    dropped["flows_outside_ecoinvent"] += 1
                    left_out[int(flow_ids[k])] += 1
                    continue
                flows_out[flow["uuid"]] = flow["label"]
                taken_flows[flow["uuid"]] += value / production
                if flow["how"] == "name":
                    dropped["flow_amounts_named_by_name"] += 1
            processes[col_code[j]] = {
                "name": str(meta.name),
                "reference_product": self._text(meta.reference_product),
                "unit": self._text(meta.unit),
                "location": self._text(meta.location),
                "kind": self._text(meta.type),
                "origin": self._origin(f"{meta.name} {self._text(meta.reference_product)}"),
                "processes": sorted(taken_processes.items()),
                "datasets": sorted(taken_datasets.items()),
                "flows": sorted(taken_flows.items()),
            }

        release = s.background_release
        payload = {
            "format": self.FORMAT,
            "agribalyse_version": s.agribalyse_version,
            "ecoinvent": {"version": release.version, "system_model": release.system_model},
            "scoring_package": self.package.content_hash,
            "flows": dict(sorted(flows_out.items())),
            "processes": dict(sorted(processes.items())),
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        report = {
            "path": str(target),
            "ecoinvent": release.version,
            "processes": len(processes),
            "by_origin": dict(Counter(p["origin"] for p in processes.values())),
            "process_inputs": sum(len(p["processes"]) for p in processes.values()),
            "dataset_inputs": sum(len(p["datasets"]) for p in processes.values()),
            "datasets": len({d for p in processes.values() for d, _ in p["datasets"]}),
            "flow_amounts": sum(len(p["flows"]) for p in processes.values()),
            "flows": len(flows_out),
            **{key: int(value) for key, value in sorted(dropped.items())},
            "flows_left_out": self._left_out(left_out),
        }
        self._log.info(
            "linked_inventory.exported",
            **{key: value for key, value in report.items() if key != "flows_left_out"},
        )
        return report

    LEFT_OUT_SAMPLE: ClassVar[int] = 40

    def _left_out(self, counts: Counter[int]) -> list[dict[str, Any]]:
        """The elementary flows left out, most used first: what a store holding
        only ecoinvent's flows cannot characterise."""
        if not counts:
            return []
        catalog = pd.read_parquet(self.settings.paths.registry_biosphere_catalog)
        names = {
            ExchangeFrameBuilder.flow_id_for((str(row.database), str(row.code))): row
            for row in catalog.itertuples(index=False)
        }
        out = []
        for flow_id, n in counts.most_common(self.LEFT_OUT_SAMPLE):
            row = names.get(flow_id)
            out.append(
                {
                    "exchanges": n,
                    "database": str(row.database) if row is not None else "",
                    "code": str(row.code) if row is not None else str(flow_id),
                    "name": str(getattr(row, "name", "")) if row is not None else "",
                    "categories": self._listed(getattr(row, "categories", None)),
                }
            )
        return out

    # ------------------------------------------------------------------

    @staticmethod
    def _ids_by_index(id_to_idx: dict[int, int]) -> np.ndarray:
        out = np.zeros(len(id_to_idx), dtype="int64")
        for identifier, index in id_to_idx.items():
            out[index] = identifier
        return out

    @staticmethod
    def _listed(value: Any) -> list[str]:
        """A parquet list cell (a numpy array, or nothing) as a list of strings."""
        return [] if value is None else [str(item) for item in value]

    @staticmethod
    def _text(value: Any) -> str:
        return "" if value is None or pd.isna(value) else str(value)

    def _origin(self, name: str) -> str:
        """Whose data a process is, from the marker ADEME puts in the name of its
        product (the process name itself does not always carry it)."""
        if self.ADAPTED.search(name):
            return "ecoinvent_adapted"
        if self.COPIED.search(name):
            return "ecoinvent_copy"
        return "agribalyse"

    def _dataset_identifiers(self) -> dict[tuple[str, str], str]:
        """``(database, code)`` of a background dataset -> ``<activity id>_<product id>``."""
        release = self.settings.background_release
        path = self.settings.paths.release_activities_json(release)
        rows = json.loads(path.read_text(encoding="utf-8"))["activities"]
        if rows and not rows[0].get("activity_uuid"):
            raise ValueError(
                f"{path.name} holds no published ids: rewrite it with "
                f"`dds-bootstrap-ecoinvent --ecoinvent-version {release.version}`."
            )
        return {
            (release.db_name, row["code"]): f"{row['activity_uuid']}_{row['product_uuid']}"
            for row in rows
        }

    def _flow_keys(self) -> dict[int, dict[str, Any]]:
        """Biosphere row id -> the background elementary flow it stands for.

        By id first: a row of the linking namespace is followed through the
        bridge, a ``biosphere3`` row is taken as it is since ``biosphere3``
        reuses ecoinvent's flow ids, and a regional row (``<id>@<region>``) is
        its base flow. Then by name and compartment (``BackgroundFlowResolver``)
        for the rows no id leads anywhere. Rows with no ecoinvent flow behind
        them are absent.
        """
        s = self.settings
        release = s.background_release
        flows = self._flows(release)
        labels = {flow["code"]: self._label(flow) for flow in flows}
        to_background = dict(ReleaseBridge.load(s).flows) if s.has_background_release else None
        resolver = BackgroundFlowResolver.load(
            flows,
            self._flows(s.linking_release) if s.has_background_release else (),
            s.paths.background_flow_synonyms_json,
        )
        wanted = self.package.biosphere.row_id_to_idx
        out: dict[int, dict[str, Any]] = {}
        catalog = pd.read_parquet(s.paths.registry_biosphere_catalog)
        for row in catalog.itertuples(index=False):
            database, code = str(row.database), str(row.code)
            flow_id = ExchangeFrameBuilder.flow_id_for((database, code))
            if flow_id not in wanted:
                continue
            base = code.split(self.REGION_SEPARATOR, 1)[0]
            if database == s.biosphere_db_name and to_background is not None:
                uuid = to_background.get(base)
            elif database in (s.biosphere_db_name, "biosphere3", release.biosphere_db_name):
                uuid = base
            else:
                uuid = None
            how = "id"
            if uuid is None or uuid not in labels:
                uuid = resolver.resolve(
                    str(getattr(row, "name", "")),
                    self._listed(getattr(row, "categories", None)),
                    getattr(row, "unit", None),
                )
                how = "name"
            if uuid is not None:
                out[flow_id] = {"uuid": uuid, "label": labels[uuid], "how": how}
        for code, label in labels.items():
            # Flows the background release introduced are in no catalog.
            flow_id = ExchangeFrameBuilder.flow_id_for((release.biosphere_db_name, code))
            if flow_id in wanted:
                out.setdefault(flow_id, {"uuid": code, "label": label, "how": "id"})
        return out

    def _flows(self, release: Any) -> list[dict[str, Any]]:
        path = self.settings.paths.release_biosphere_flows_json(release)
        if not path.exists():
            return []
        return json.loads(path.read_text(encoding="utf-8"))["flows"]

    def _label(self, flow: dict[str, Any]) -> dict[str, str]:
        categories = list(flow.get("categories") or ())
        return {
            "name": str(flow.get("name") or ""),
            "compartment": str(categories[0]) if categories else "",
            "sub_compartment": str(categories[1]) if len(categories) > 1 else self.UNSPECIFIED,
            "unit": str(flow.get("unit") or ""),
        }
