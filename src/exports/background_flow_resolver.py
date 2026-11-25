"""``BackgroundFlowResolver`` — name, in an ecoinvent release, a flow that is not its own.

The linker gives every Agribalyse elementary flow the best target it knows:
a flow of the linking release's biosphere when there is one, a flow of the
EF list otherwise. A store that holds only an ecoinvent release cannot
characterise the latter, nor a flow the release has since dropped. Most of
them do have a counterpart there, though: ecoinvent keeps adding substances
(pesticides above all), and renames more than it removes.

``resolve`` finds that counterpart, on names and compartments only:

* the name, case-insensitive, after the release's own renamings (read off
  the flows both releases share an id for) and a short curated list of
  synonyms (``source/ef-flow-names-in-ecoinvent.json``: IUPAC names the EF
  list spells out, ``total`` nitrogen and phosphorus, unspecified chromium);
* the compartment, translated from the EF context where needed, then its
  ``unspecified`` sub-compartment when the release has no such flow in the
  exact one;
* the same unit, and one candidate only: anything else resolves to nothing.

It never crosses a compartment (air to soil) and never converts a unit.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


@dataclass(frozen=True)
class BackgroundFlowResolver:
    flows: tuple[dict[str, Any], ...]
    """Elementary flows of the release to name flows in (its snapshot rows)."""
    renamed: Mapping[str, str] = field(default_factory=dict)
    """Lower-case name in the linking release -> name in this release."""
    synonyms: Mapping[str, str] = field(default_factory=dict)
    """Lower-case name outside ecoinvent -> lower-case name in this release."""

    EF_CONTEXTS: ClassVar[Mapping[str, tuple[str, ...]]] = {
        "emissions to air, unspecified": ("air",),
        "emissions to air, unspecified (long-term)": ("air", "low population density, long-term"),
        "emissions to non-urban air or from high stacks": (
            "air",
            "non-urban air or from high stacks",
        ),
        "emissions to urban air close to ground": ("air", "urban air close to ground"),
        "emissions to lower stratosphere and upper troposphere": (
            "air",
            "lower stratosphere + upper troposphere",
        ),
        "emissions to air, indoor": ("air", "indoor"),
        "emissions to fresh water": ("water", "surface water"),
        "emissions to sea water": ("water", "ocean"),
        "emissions to water, unspecified": ("water",),
        "emissions to water, unspecified (long-term)": ("water", "ground-, long-term"),
        "emissions to agricultural soil": ("soil", "agricultural"),
        "emissions to non-agricultural soil": ("soil", "industrial"),
        "emissions to soil, unspecified": ("soil",),
    }

    @classmethod
    def load(
        cls,
        flows: Iterable[dict[str, Any]],
        linking_flows: Iterable[dict[str, Any]] = (),
        synonyms_path: Path | None = None,
    ) -> BackgroundFlowResolver:
        """Build the resolver for ``flows``; ``linking_flows`` give the renamings."""
        flows = tuple(flows)
        by_code = {flow["code"]: flow for flow in flows}
        renamed: dict[str, str] = {}
        for flow in linking_flows:
            continued = by_code.get(flow["code"])
            if continued is not None:
                old, new = cls._name(flow.get("name")), cls._name(continued.get("name"))
                if old != new:
                    renamed[old] = new
        synonyms: dict[str, str] = {}
        if synonyms_path is not None and synonyms_path.exists():
            payload = json.loads(synonyms_path.read_text(encoding="utf-8"))
            synonyms = {cls._name(k): cls._name(v) for k, v in payload["names"].items()}
        return cls(flows=flows, renamed=renamed, synonyms=synonyms)

    def resolve(self, name: str, categories: Iterable[str], unit: str | None) -> str | None:
        """Code of the flow standing for ``(name, categories, unit)``, or ``None``."""
        compartments = self._compartments(tuple(str(c) for c in categories))
        if compartments is None:
            return None
        wanted_unit = self._name(unit)
        for candidate in self._names(name):
            for context in (compartments, compartments[:1]):
                hits = [
                    flow
                    for flow in self._index.get((candidate, context), ())
                    if not wanted_unit or self._name(flow.get("unit")) == wanted_unit
                ]
                if len(hits) == 1:
                    return hits[0]["code"]
                if hits:
                    return None
        return None

    # ------------------------------------------------------------------

    @property
    def _index(self) -> dict[tuple[str, tuple[str, ...]], list[dict[str, Any]]]:
        index = self.__dict__.get("_index_cache")
        if index is None:
            index = {}
            for flow in self.flows:
                key = (
                    self._name(flow.get("name")),
                    tuple(self._name(c) for c in flow.get("categories") or ()),
                )
                index.setdefault(key, []).append(flow)
            object.__setattr__(self, "_index_cache", index)
        return index

    def _names(self, name: str) -> tuple[str, ...]:
        """The name itself, then what it was renamed to, then its synonym."""
        base = self._name(name)
        out = [base]
        for table in (self.renamed, self.synonyms):
            other = table.get(base)
            if other and other not in out:
                out.append(other)
        return tuple(out)

    def _compartments(self, categories: tuple[str, ...]) -> tuple[str, ...] | None:
        """ecoinvent compartments of a flow's categories: translated when they
        are an EF context (``Emissions / Emissions to water / Emissions to
        fresh water``), taken as they are otherwise."""
        if not categories:
            return None
        if self._name(categories[0]) in ("emissions", "resources", "land use"):
            return self.EF_CONTEXTS.get(self._name(categories[-1]))
        return tuple(self._name(c) for c in categories)

    @staticmethod
    def _name(value: Any) -> str:
        return str(value or "").strip().lower()
