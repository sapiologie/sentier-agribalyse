"""``LinkedInventoryExporter`` and ``BackgroundCfSupplement`` on a three-column system.

columns: bread (Agribalyse), flour (Agribalyse), steel (background dataset)
bread makes 2 kg from 1 kg of flour and 0.4 kg of steel, emitting 4 kg of CO2,
      6 m3 of French water and 1 kg of a flow only the EF list knows
flour makes 1 kg and avoids 0.1 kg of steel
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import sparse as sp

from ef.background_cfs import BackgroundCfSupplement
from exports.linked_inventory import LinkedInventoryExporter
from scoring.exchange_frame_builder import ExchangeFrameBuilder
from scoring.matrix_builder import BuiltMatrix
from scoring.scoring_package import ScoringPackage
from transforms.background_rebase import ReleaseBridge

AGB = "agribalyse-3.2"
flow_id = ExchangeFrameBuilder.flow_id_for


def _package(settings, biosphere_db: str, co2_code: str) -> ScoringPackage:
    background = settings.background_release.db_name
    columns = [flow_id((AGB, "bread")), flow_id((AGB, "flour")), flow_id((background, "steel"))]
    a = sp.csr_matrix(np.array([[2.0, 0.0, 0.0], [-1.0, 1.0, 0.0], [-0.4, 0.1, 1.0]]))
    flows = [
        flow_id((biosphere_db, co2_code)),
        flow_id((biosphere_db, "f-water@FR")),
        flow_id(("ef", "f-ef-only")),
    ]
    b = sp.csr_matrix(np.array([[4.0, 0.0, 7.0], [6.0, 0.0, 0.0], [1.0, 0.0, 0.0]]))
    col_map = {identifier: index for index, identifier in enumerate(columns)}
    return ScoringPackage(
        technosphere=BuiltMatrix(matrix=a, row_id_to_idx=dict(col_map), col_id_to_idx=col_map),
        biosphere=BuiltMatrix(
            matrix=b,
            row_id_to_idx={identifier: index for index, identifier in enumerate(flows)},
            col_id_to_idx=col_map,
        ),
        methods={},
        content_hash="three-columns",
    )


def _write_artifacts(settings, biosphere_db: str, catalog_codes: list[str]) -> None:
    paths = settings.paths
    release = settings.background_release
    for directory in (paths.source, paths.registry):
        directory.mkdir(parents=True, exist_ok=True)
    rows = [
        (
            flow_id((AGB, "bread")),
            AGB,
            "bread",
            "Bread {FR} U",
            "process",
            "kilogram",
            "FR",
            "bread",
        ),
        (
            flow_id((AGB, "flour")),
            AGB,
            "flour",
            "Flour",
            "process",
            "kilogram",
            None,
            "Flour {RER}| milling | Cut-off, U - Adapted from Ecoinvent U",
        ),
        (
            flow_id((release.db_name, "steel")),
            release.db_name,
            "steel",
            "steel production",
            "process",
            "kilogram",
            "RoW",
            "steel",
        ),
    ]
    catalog = pd.DataFrame(
        rows,
        columns=[
            "activity_id",
            "database",
            "code",
            "name",
            "type",
            "unit",
            "location",
            "reference_product",
        ],
    )
    catalog["product_id"] = catalog["activity_id"]
    catalog.to_parquet(paths.registry_activity_catalog)
    pd.DataFrame(
        [(biosphere_db, code, code, ["air"], "kilogram") for code in catalog_codes]
        + [("ef", "f-ef-only", "Flow of the EF list", ["soil"], "kilogram")],
        columns=["database", "code", "name", "categories", "unit"],
    ).to_parquet(paths.registry_biosphere_catalog)
    paths.release_activities_json(release).write_text(
        json.dumps(
            {
                "db_name": release.db_name,
                "activities": [
                    {"code": "steel", "activity_uuid": "act-1", "product_uuid": "prod-1"}
                ],
            }
        )
    )


def _flow(code: str, name: str, categories: list[str], unit: str = "kilogram") -> dict:
    return {"code": code, "name": name, "categories": categories, "unit": unit}


class TestWithoutBackgroundRelease:
    @pytest.fixture
    def exported(self, settings, tmp_path: Path) -> tuple[dict, dict]:
        _write_artifacts(settings, settings.biosphere_db_name, ["f-co2", "f-water", "f-water@FR"])
        settings.paths.release_biosphere_flows_json(settings.background_release).write_text(
            json.dumps(
                {
                    "flows": [
                        _flow(
                            "f-co2", "Carbon dioxide, fossil", ["air", "urban air close to ground"]
                        ),
                        _flow("f-water", "Water", ["water"], "cubic meter"),
                    ]
                }
            )
        )
        target = tmp_path / "links.json"
        report = LinkedInventoryExporter(
            settings=settings, package=_package(settings, settings.biosphere_db_name, "f-co2")
        ).export(target)
        return json.loads(target.read_text()), report

    def test_amounts_are_per_unit_of_reference_product(self, exported):
        payload, _ = exported
        bread = payload["processes"]["bread"]
        assert bread["processes"] == [["flour", 0.5]]
        assert bread["datasets"] == [["act-1_prod-1", 0.2]]
        assert dict(bread["flows"]) == {"f-co2": 2.0, "f-water": 3.0}

    def test_an_avoided_product_is_a_negative_input(self, exported):
        payload, _ = exported
        assert payload["processes"]["flour"]["datasets"] == [["act-1_prod-1", -0.1]]

    def test_background_datasets_are_not_processes_of_the_file(self, exported):
        payload, report = exported
        assert set(payload["processes"]) == {"bread", "flour"}
        assert report["datasets"] == 1

    def test_flows_carry_ecoinvent_compartments(self, exported):
        payload, _ = exported
        assert payload["flows"] == {
            "f-co2": {
                "name": "Carbon dioxide, fossil",
                "compartment": "air",
                "sub_compartment": "urban air close to ground",
                "unit": "kilogram",
            },
            "f-water": {
                "name": "Water",
                "compartment": "water",
                "sub_compartment": "unspecified",
                "unit": "cubic meter",
            },
        }

    def test_flows_outside_ecoinvent_are_counted_not_written(self, exported):
        payload, report = exported
        assert report["flows_outside_ecoinvent"] == 1
        assert report["flows_left_out"] == [
            {
                "exchanges": 1,
                "database": "ef",
                "code": "f-ef-only",
                "name": "Flow of the EF list",
                "categories": ["soil"],
            }
        ]
        assert "f-ef-only" not in payload["flows"]

    def test_a_flow_of_the_ef_list_is_named_by_the_ecoinvent_flow_of_its_substance(
        self, settings, tmp_path
    ):
        _write_artifacts(settings, settings.biosphere_db_name, ["f-co2", "f-water", "f-water@FR"])
        biosphere_db = settings.biosphere_db_name
        pd.DataFrame(
            [
                (biosphere_db, "f-co2", "Carbon dioxide, fossil", ["air"], "kilogram"),
                (
                    "ef",
                    "f-ef-only",
                    "nitrogen, total (excluding N2)",
                    ["Emissions", "Emissions to water", "Emissions to fresh water"],
                    "kilogram",
                ),
            ],
            columns=["database", "code", "name", "categories", "unit"],
        ).to_parquet(settings.paths.registry_biosphere_catalog)
        settings.paths.background_flow_synonyms_json.write_text(
            json.dumps({"names": {"nitrogen, total (excluding n2)": "nitrogen"}})
        )
        settings.paths.release_biosphere_flows_json(settings.background_release).write_text(
            json.dumps(
                {
                    "flows": [
                        _flow("f-co2", "Carbon dioxide, fossil", ["air"]),
                        _flow("f-n", "Nitrogen", ["water", "surface water"]),
                    ]
                }
            )
        )
        target = tmp_path / "links.json"

        report = LinkedInventoryExporter(
            settings=settings, package=_package(settings, settings.biosphere_db_name, "f-co2")
        ).export(target)

        bread = json.loads(target.read_text())["processes"]["bread"]
        assert dict(bread["flows"])["f-n"] == 0.5
        assert report["flow_amounts_named_by_name"] == 1

    def test_origin_tells_the_embedded_ecoinvent_copies_apart(self, exported):
        payload, report = exported
        assert payload["processes"]["bread"]["origin"] == "agribalyse"
        assert payload["processes"]["flour"]["origin"] == "ecoinvent_adapted"
        assert report["by_origin"] == {"agribalyse": 1, "ecoinvent_adapted": 1}

    def test_states_the_release_it_names(self, exported, settings):
        payload, _ = exported
        assert payload["ecoinvent"] == {
            "version": settings.linking_release.version,
            "system_model": "cutoff",
        }


class TestWithBackgroundRelease:
    @pytest.fixture
    def bridged(self, settings):
        settings = replace(settings, background_ecoinvent_version="99.0")
        _write_artifacts(settings, settings.biosphere_db_name, ["f-old", "f-water", "f-water@FR"])
        settings.paths.release_biosphere_flows_json(settings.background_release).write_text(
            json.dumps(
                {
                    "flows": [
                        _flow("f-new", "Carbon dioxide, renamed", ["air"]),
                        _flow("f-water", "Water", ["water"], "cubic meter"),
                        _flow("f-only-new", "Novel gas", ["air"]),
                    ]
                }
            )
        )
        pd.DataFrame(
            [("x", "steel", 1.0, "", "")],
            columns=["old_code", "new_code", "share", "old_label", "new_label"],
        ).to_parquet(settings.paths.registry_release_bridge_activities)
        pd.DataFrame(
            [("f-old", "f-new", "name"), ("f-water", "f-water", "id")],
            columns=["old_code", "new_code", "how"],
        ).to_parquet(settings.paths.registry_release_bridge_flows)
        return settings

    def test_names_the_flows_and_datasets_of_the_background_release(self, bridged, tmp_path):
        target = tmp_path / "links.json"
        LinkedInventoryExporter(
            settings=bridged, package=_package(bridged, bridged.biosphere_db_name, "f-old")
        ).export(target)
        payload = json.loads(target.read_text())
        assert payload["ecoinvent"]["version"] == "99.0"
        assert dict(payload["processes"]["bread"]["flows"]) == {"f-new": 2.0, "f-water": 3.0}
        assert payload["flows"]["f-new"]["name"] == "Carbon dioxide, renamed"
        assert payload["processes"]["bread"]["datasets"] == [["act-1_prod-1", 0.2]]

    def test_cf_supplement_adds_only_the_flows_the_background_introduced(self, bridged):
        bridged.ef_methods_snapshot_for(bridged.background_release).write_text(
            json.dumps(
                {
                    "methods": {
                        "m": {
                            "key": ["ecoinvent-99.0", "EF v3.1", "climate change", "GWP100"],
                            "inherited_cfs": [
                                {"db": "ecoinvent-99.0-biosphere", "code": "f-new", "amount": 1.0},
                                {
                                    "db": "ecoinvent-99.0-biosphere",
                                    "code": "f-only-new",
                                    "amount": 25.0,
                                },
                            ],
                        }
                    }
                }
            )
        )
        paired = (bridged.linking_release.method_prefix, "EF v3.1", "climate change", "GWP100")
        unpaired = (bridged.linking_release.method_prefix, "EF v3.1", "water use", "AWARE")
        table = pd.DataFrame(
            [(bridged.biosphere_db_name, "f-old", 1.0)], columns=["database", "code", "amount"]
        )
        out, report = BackgroundCfSupplement(
            settings=bridged, bridge=ReleaseBridge.load(bridged)
        ).apply({paired: table, unpaired: table})
        added = out[paired].iloc[1:]
        assert added.to_dict("records") == [
            {"database": "ecoinvent-99.0-biosphere", "code": "f-only-new", "amount": 25.0}
        ]
        assert len(out[unpaired]) == 1
        assert report["factors_added"] == 1
        assert report["methods_unpaired"] == [" / ".join(unpaired)]
