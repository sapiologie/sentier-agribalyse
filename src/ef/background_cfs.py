"""``BackgroundCfSupplement`` — factors for the flows only the background release has.

With a background release, the elementary flows of its exchanges are
brought into the linking release's namespace wherever a flow continues one
of that release (``ReleaseBridge.background_flow_renames``): those keep the
factors of the CF registry. A flow the background release introduced has no
counterpart there, so the registry holds no factor for it. This class adds,
for each method, the factors the background release itself publishes for
those flows (its EF snapshot, written by ``dds-bootstrap-ecoinvent
--ecoinvent-version``), and nothing else.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pandas as pd

from config import Settings
from core.logging import Logging
from transforms.background_rebase import ReleaseBridge


@dataclass(frozen=True)
class BackgroundCfSupplement:
    settings: Settings
    bridge: ReleaseBridge

    @property
    def _log(self) -> Any:
        return Logging.get(__name__)

    def apply(
        self, method_cfs: dict[tuple[str, ...], pd.DataFrame]
    ) -> tuple[dict[tuple[str, ...], pd.DataFrame], dict[str, Any]]:
        """Return ``method_cfs`` with the background-only factors appended, and a report.

        Methods are paired on everything but the release prefix of their key
        (``("ecoinvent-3.9.1", "EF v3.1", "acidification", ...)`` with
        ``("ecoinvent-3.13", "EF v3.1", "acidification", ...)``).
        """
        background = self._background_methods()
        continued = set(self.bridge.flows.values())
        out: dict[tuple[str, ...], pd.DataFrame] = {}
        unpaired: list[str] = []
        n_added = 0
        for key, table in method_cfs.items():
            rows = background.get(tuple(key[1:]))
            if rows is None:
                unpaired.append(" / ".join(key))
                out[key] = table
                continue
            extra = pd.DataFrame(
                [
                    (row["db"], row["code"], float(row["amount"]))
                    for row in rows
                    if row["db"] == self.bridge.background_biosphere_db
                    and row["code"] not in continued
                ],
                columns=["database", "code", "amount"],
            )
            n_added += len(extra)
            out[key] = pd.concat([table, extra], ignore_index=True) if len(extra) else table
        report = {"methods": len(out), "factors_added": n_added, "methods_unpaired": unpaired}
        self._log.info("background.cfs.supplemented", **report)
        return out, report

    def _background_methods(self) -> dict[tuple[str, ...], list[dict[str, Any]]]:
        path = self.settings.ef_methods_snapshot_for(self.settings.background_release)
        if not path.exists():
            raise FileNotFoundError(
                f"{path} is missing: run `dds-bootstrap-ecoinvent --ecoinvent-version "
                f"{self.settings.background_release.version}` first."
            )
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        return {
            tuple(method["key"][1:]): method["inherited_cfs"]
            for method in snapshot["methods"].values()
        }
