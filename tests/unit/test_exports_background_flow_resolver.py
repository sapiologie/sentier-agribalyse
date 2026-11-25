"""``BackgroundFlowResolver`` — a flow that is not the release's own, named in it."""

from __future__ import annotations

import json

import pytest

from exports.background_flow_resolver import BackgroundFlowResolver


def _flow(code: str, name: str, categories: list[str], unit: str = "kilogram") -> dict:
    return {"code": code, "name": name, "categories": categories, "unit": unit}


RELEASE = [
    _flow("n-water", "Nitrogen", ["water"]),
    _flow("n-surface", "Nitrogen", ["water", "surface water"]),
    _flow("n-soil", "Nitrogen", ["soil"]),
    _flow("cr3-water", "Chromium III", ["water", "surface water"]),
    _flow("gas-air", "Hexafluoroethane", ["air"]),
    _flow("dust-air", "Dust", ["air"]),
    _flow("dust-air-2", "Dust", ["air"]),
    _flow("radon", "Radon-222", ["air"], "kilo Becquerel"),
]
EF_FRESH_WATER = ["Emissions", "Emissions to water", "Emissions to fresh water"]


@pytest.fixture
def resolver(tmp_path) -> BackgroundFlowResolver:
    synonyms = tmp_path / "synonyms.json"
    synonyms.write_text(
        json.dumps(
            {"names": {"Nitrogen, total (excluding N2)": "nitrogen", "chromium": "Chromium III"}}
        )
    )
    linking = [_flow("gas-air", "Ethane, hexafluoro-, HFC-116", ["air"])]
    return BackgroundFlowResolver.load(RELEASE, linking, synonyms)


class TestResolve:
    def test_same_name_in_the_translated_ef_compartment(self, resolver):
        assert resolver.resolve("nitrogen", EF_FRESH_WATER, "kilogram") == "n-surface"

    def test_falls_back_on_the_unspecified_sub_compartment(self, resolver):
        sea = ["Emissions", "Emissions to water", "Emissions to sea water"]
        assert resolver.resolve("nitrogen", sea, "kilogram") == "n-water"

    def test_a_synonym_names_what_the_ef_list_spells_differently(self, resolver):
        assert (
            resolver.resolve("nitrogen, total (excluding N2)", EF_FRESH_WATER, "kilogram")
            == "n-surface"
        )
        assert resolver.resolve("chromium", EF_FRESH_WATER, "kilogram") == "cr3-water"

    def test_a_dropped_flow_follows_the_renaming_of_its_siblings(self, resolver):
        dropped = ["air", "lower stratosphere + upper troposphere"]
        assert resolver.resolve("Ethane, hexafluoro-, HFC-116", dropped, "kilogram") == "gas-air"

    def test_never_crosses_a_compartment(self, resolver):
        soil = ["Emissions", "Emissions to soil", "Emissions to agricultural soil"]
        assert resolver.resolve("chromium", soil, "kilogram") is None

    def test_never_converts_a_unit(self, resolver):
        assert resolver.resolve("Radon-222", ["air"], "kilogram") is None
        assert resolver.resolve("Radon-222", ["air"], "kilo Becquerel") == "radon"

    def test_two_candidates_resolve_to_nothing(self, resolver):
        assert resolver.resolve("Dust", ["air"], "kilogram") is None

    def test_an_unknown_ef_context_resolves_to_nothing(self, resolver):
        resources = ["Resources", "Resources from ground", "Non-renewable element resources"]
        assert resolver.resolve("nitrogen", resources, "kilogram") is None

    def test_an_unknown_name_resolves_to_nothing(self, resolver):
        assert resolver.resolve("unobtainium", EF_FRESH_WATER, "kilogram") is None


class TestShippedSynonyms:
    def test_the_list_is_lower_case_names_only(self):
        from config import Paths

        payload = json.loads(Paths().background_flow_synonyms_json.read_text(encoding="utf-8"))
        assert payload["names"]
        for source, target in payload["names"].items():
            assert source == source.lower() and target == target.lower()
            assert source != target
