"""``dds-build-release-bridge`` and ``BackgroundRebaser`` on made-up releases.

Two invented releases, 9.1 and 11, with a patch release 10.1 in between that
has no workbook of its own. No ecoinvent name, id or amount is used.
"""

from __future__ import annotations

import json
import types
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest
from openpyxl import Workbook

from bootstrap.release_bridge import (
    CorrespondenceChain,
    CorrespondenceFileReader,
    FlowBridge,
    ReleaseBridgeBuilder,
    ReleaseBridgeError,
)
from config import EcoinventRelease
from transforms.background_rebase import BackgroundRebaser, ReleaseBridge, ReleaseBridgeMissingError

SIDE = [
    "Activity UUID",
    "ISIC Classification",
    "Special Activity Type",
    "Activity Name",
    "Geography",
    "Time Period - Start Date",
    "Time Period - End Date",
    "Technology Level",
    "Product UUID",
    "Product Name",
    "Unit",
    "CPC Classification",
    "Product Classification",
]
TAIL = [f"flag {i}" for i in range(13)] + ["Dataset count", "same", "deleted", "new"]
HEADER = [*SIDE, *SIDE, *TAIL, "Replacement amount", "Status", "Comment"]


def _side(dataset: tuple[str, str, str, str, str] | None) -> list:
    if dataset is None:
        return [None] * len(SIDE)
    activity, name, geography, product, product_name = dataset
    return [activity, "", "", name, geography, "", "", "", product, product_name, "kg", "", ""]


def _workbook(path: Path, old: str, new: str, rows: list[tuple]) -> None:
    workbook = Workbook()
    workbook.active.title = "Read me"
    sheet = workbook.create_sheet("Cut-off")
    sheet.append([old, *[None] * 12, new])
    sheet.append(HEADER)
    for old_dataset, new_dataset, share in rows:
        sheet.append([*_side(old_dataset), *_side(new_dataset), *[None] * len(TAIL), share, "", ""])
    workbook.save(path)


WHEAT = ("a-wheat", "wheat growing", "FR", "p-wheat", "wheat")
STEEL = ("a-steel", "steel making", "RoW", "p-steel", "steel")
STEEL_CN = ("a-steel-cn", "steel making", "CN", "p-steel", "steel")
STEEL_ROW = ("a-steel-row", "steel making", "RoW", "p-steel", "steel")
PEAT = ("a-peat", "peat cutting", "GLO", "p-peat", "peat")
LIME = ("a-lime", "lime burning", "GLO", "p-lime", "lime")
LIME_REISSUED = ("a-lime-2", "lime burning", "GLO", "p-lime", "lime")
STEEL_CN_N = ("a-steel-cn-n", "steel making", "CN-N", "p-steel", "steel")
STEEL_CN_S = ("a-steel-cn-s", "steel making", "CN-S", "p-steel", "steel")
GLASS = ("a-glass", "glass melting", "GLO", "p-glass", "glass")


@pytest.fixture
def correspondence_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "correspondence"
    directory.mkdir()
    _workbook(
        directory / "Correspondence File v9.1 - v.10.xlsx",
        "9.1",
        "10",
        [
            (WHEAT, WHEAT, 1),
            (STEEL, STEEL_CN, 0.25),
            (STEEL, STEEL_ROW, 0.75),
            (PEAT, None, None),
            (LIME, LIME, 1),
            (None, GLASS, None),
        ],
    )
    _workbook(
        directory / "Correspondence File v10.1 - v11.xlsx",
        "10.1",
        "11",
        [
            (WHEAT, WHEAT, 1),
            (STEEL_CN, STEEL_CN_N, 0.4),
            (STEEL_CN, STEEL_CN_S, 0.6),
            (STEEL_ROW, STEEL_ROW, 1),
            (LIME_REISSUED, LIME_REISSUED, 1),
            (GLASS, GLASS, 1),
        ],
    )
    return directory


def _activities(db_name: str, datasets: list[tuple]) -> dict:
    return {
        "db_name": db_name,
        "activities": [
            {"code": f"code-{d[0]}", "activity_uuid": d[0], "product_uuid": d[3]} for d in datasets
        ],
    }


@pytest.fixture
def bridged_settings(settings, tmp_path: Path):
    """Linking release 9.1, background release 11, both snapshotted."""
    settings = replace(settings, ecoinvent_version="9.1", background_ecoinvent_version="11")
    old, new = settings.linking_release, settings.background_release
    paths = settings.paths
    paths.source.mkdir(parents=True, exist_ok=True)
    paths.release_activities_json(old).write_text(
        json.dumps(_activities(old.db_name, [WHEAT, STEEL, PEAT, LIME]))
    )
    paths.release_activities_json(new).write_text(
        json.dumps(
            _activities(
                new.db_name, [WHEAT, STEEL_CN_N, STEEL_CN_S, STEEL_ROW, LIME_REISSUED, GLASS]
            )
        )
    )
    flow = lambda code, name: {"code": code, "name": name, "categories": ["air"], "unit": "kg"}  # noqa: E731
    paths.release_biosphere_flows_json(old).write_text(
        json.dumps(
            {
                "flows": [
                    flow("f-co2", "Carbon dioxide"),
                    flow("f-old", "Dust"),
                    flow("f-gone", "Smoke"),
                ]
            }
        )
    )
    paths.release_biosphere_flows_json(new).write_text(
        json.dumps(
            {
                "flows": [
                    flow("f-co2", "Carbon dioxide, renamed"),
                    flow("f-new", "Dust"),
                    flow("f-extra", "Fog"),
                ]
            }
        )
    )
    return settings


class TestCorrespondenceFileReader:
    def test_reads_the_release_pair_from_the_file_name(self):
        reader = CorrespondenceFileReader
        assert reader.versions_of(Path("Correspondence File v3.9.1 - v.3.10.xlsx")) == (
            "3.9.1",
            "3.10",
        )
        assert reader.versions_of(Path("Correspondence File v3.12 - v3.13.xlsx")) == (
            "3.12",
            "3.13",
        )
        assert reader.versions_of(Path("Database-Overview.xlsx")) is None

    def test_keeps_removed_datasets_and_skips_added_ones(self, correspondence_dir):
        step = CorrespondenceFileReader().read(
            correspondence_dir / "Correspondence File v9.1 - v.10.xlsx"
        )
        assert (step.old_version, step.new_version) == ("9.1", "10")
        assert len(step.rows) == 5
        peat = step.rows[step.rows["old_activity_uuid"] == "a-peat"].iloc[0]
        assert pd.isna(peat["new_activity_uuid"]) and peat["share"] == 0.0

    def test_a_sheet_of_another_system_model_is_reported(self, correspondence_dir):
        with pytest.raises(ReleaseBridgeError, match="no sheet"):
            CorrespondenceFileReader(system_model="apos").read(
                correspondence_dir / "Correspondence File v9.1 - v.10.xlsx"
            )


class TestCorrespondenceChain:
    def test_multiplies_the_shares_along_the_chain(self, correspondence_dir):
        chain = CorrespondenceChain.discover(
            correspondence_dir, "9.1", "11", CorrespondenceFileReader()
        )
        bridge, steps = chain.compose()
        shares = {
            (row.old_activity_uuid, row.new_activity_uuid): row.share for row in bridge.itertuples()
        }
        assert shares == pytest.approx(
            {
                ("a-wheat", "a-wheat"): 1.0,
                ("a-steel", "a-steel-cn-n"): 0.25 * 0.4,
                ("a-steel", "a-steel-cn-s"): 0.25 * 0.6,
                ("a-steel", "a-steel-row"): 0.75,
                ("a-lime", "a-lime-2"): 1.0,
            }
        )
        assert [(s["from"], s["to"]) for s in steps] == [("9.1", "10"), ("10.1", "11")]

    def test_joins_on_the_label_where_a_patch_release_reissued_the_ids(self, correspondence_dir):
        chain = CorrespondenceChain.discover(
            correspondence_dir, "9.1", "11", CorrespondenceFileReader()
        )
        _, steps = chain.compose()
        assert steps[1]["joined_by_label"] == 1

    def test_a_missing_step_names_the_release_it_stops_at(self, correspondence_dir):
        with pytest.raises(ReleaseBridgeError, match="starts at ecoinvent 11"):
            CorrespondenceChain.discover(
                correspondence_dir, "9.1", "12", CorrespondenceFileReader()
            )


class TestFlowBridge:
    def test_pairs_by_id_then_by_name(self):
        flow = lambda code, name: {"code": code, "name": name, "categories": ["air"], "unit": "kg"}  # noqa: E731
        bridge = FlowBridge.build(
            [flow("a", "Carbon dioxide"), flow("b", "Dust"), flow("c", "Smoke")],
            [flow("a", "Renamed"), flow("z", "Dust")],
        )
        assert bridge.set_index("old_code")["how"].to_dict() == {
            "a": "id",
            "b": "name",
            "c": "none",
        }
        assert bridge.set_index("old_code")["new_code"].to_dict()["b"] == "z"

    def test_a_flow_whose_unit_changed_under_the_same_id_is_not_paired(self):
        old = {"code": "a", "name": "Isotope", "categories": ["air"], "unit": "kilo Becquerel"}
        new = {"code": "a", "name": "Isotope", "categories": ["air"], "unit": "kilogram"}
        bridge = FlowBridge.build([old], [new])
        assert bridge.to_dict("records") == [{"old_code": "a", "new_code": None, "how": "unit"}]


class TestReleaseBridgeBuilder:
    def test_writes_both_tables_keyed_by_brightway_codes(
        self, bridged_settings, correspondence_dir
    ):
        report = ReleaseBridgeBuilder(
            settings=bridged_settings, correspondence_dir=correspondence_dir
        ).build()
        activities = pd.read_parquet(bridged_settings.paths.registry_release_bridge_activities)
        steel = activities[activities["old_code"] == "code-a-steel"]
        assert steel["share"].sum() == pytest.approx(1.0)
        assert set(steel["new_code"]) == {
            "code-a-steel-cn-n",
            "code-a-steel-cn-s",
            "code-a-steel-row",
        }
        peat = activities[activities["old_code"] == "code-a-peat"]
        assert peat["new_code"].isna().all() and peat["share"].sum() == 0.0
        assert report["datasets"] == 4
        assert report["datasets_kept_whole"] == 3
        assert report["datasets_lost"] == 1
        assert report["datasets_split"] == 1
        assert (report["flows_by_id"], report["flows_by_name"], report["flows_lost"]) == (1, 1, 1)

    def test_a_snapshot_under_other_ids_is_matched_on_its_label(
        self, bridged_settings, correspondence_dir
    ):
        old = bridged_settings.linking_release
        reissued = {
            "code": "code-wheat-reissued",
            "activity_uuid": "another-id",
            "product_uuid": "p-wheat",
            "name": "Wheat growing",
            "location": "FR",
            "reference_product": "wheat",
        }
        bridged_settings.paths.release_activities_json(old).write_text(
            json.dumps({"db_name": old.db_name, "activities": [reissued]})
        )

        ReleaseBridgeBuilder(
            settings=bridged_settings, correspondence_dir=correspondence_dir
        ).build()

        activities = pd.read_parquet(bridged_settings.paths.registry_release_bridge_activities)
        assert activities[["old_code", "new_code", "share"]].to_dict("records") == [
            {"old_code": "code-wheat-reissued", "new_code": "code-a-wheat", "share": 1.0}
        ]

    def test_refuses_to_run_without_a_background_release(self, settings, correspondence_dir):
        with pytest.raises(ReleaseBridgeError, match="No background release"):
            ReleaseBridgeBuilder(settings=settings, correspondence_dir=correspondence_dir).build()

    def test_a_snapshot_without_published_ids_asks_for_a_new_bootstrap(
        self, bridged_settings, correspondence_dir
    ):
        old = bridged_settings.linking_release
        bridged_settings.paths.release_activities_json(old).write_text(
            json.dumps({"db_name": old.db_name, "activities": [{"code": "x"}]})
        )
        with pytest.raises(ReleaseBridgeError, match="published ids"):
            ReleaseBridgeBuilder(
                settings=bridged_settings, correspondence_dir=correspondence_dir
            ).build()


class TestBackgroundRebaser:
    @pytest.fixture
    def bridge(self, bridged_settings, correspondence_dir) -> ReleaseBridge:
        ReleaseBridgeBuilder(
            settings=bridged_settings, correspondence_dir=correspondence_dir
        ).build()
        return ReleaseBridge.load(bridged_settings)

    @staticmethod
    def _sp(exchanges: list[dict]):
        return types.SimpleNamespace(
            data=[{"database": "agribalyse-3.2", "code": "bread", "exchanges": exchanges}]
        )

    def test_relinks_splits_and_unlinks(self, bridged_settings, bridge):
        old_db = bridged_settings.linking_release.db_name
        new_db = bridged_settings.background_release.db_name
        sp = self._sp(
            [
                {"type": "production", "input": ("agribalyse-3.2", "bread"), "amount": 1.0},
                {"type": "technosphere", "input": (old_db, "code-a-wheat"), "amount": 2.0},
                {"type": "technosphere", "input": (old_db, "code-a-steel"), "amount": 10.0},
                {"type": "technosphere", "input": (old_db, "code-a-peat"), "amount": 3.0},
                {"type": "biosphere", "input": ("ecoinvent-9.1-biosphere", "f-co2"), "amount": 5.0},
            ]
        )
        report = BackgroundRebaser(settings=bridged_settings, bridge=bridge).apply(sp)
        exchanges = sp.data[0]["exchanges"]
        linked = {exc["input"]: exc["amount"] for exc in exchanges if "input" in exc}
        assert linked == pytest.approx(
            {
                ("agribalyse-3.2", "bread"): 1.0,
                (new_db, "code-a-wheat"): 2.0,
                (new_db, "code-a-steel-cn-n"): 1.0,
                (new_db, "code-a-steel-cn-s"): 1.5,
                (new_db, "code-a-steel-row"): 7.5,
                ("ecoinvent-9.1-biosphere", "f-co2"): 5.0,
            }
        )
        unlinked = [exc for exc in exchanges if "input" not in exc]
        assert [exc["amount"] for exc in unlinked] == [3.0]
        assert report["exchanges_relinked"] == 1
        assert report["exchanges_split"] == 1
        assert report["exchanges_lost"] == 1
        assert report["not_whole_sample"] == [{"code": "code-a-peat", "exchanges": 1}]

    def test_the_remaining_successors_take_the_whole_input(self, bridged_settings, bridge):
        """Steel lost a quarter of itself on the way: what is left takes all of it."""
        old_db = bridged_settings.linking_release.db_name
        new_db = bridged_settings.background_release.db_name
        partly = ReleaseBridge(
            linking_db=bridge.linking_db,
            background_db=bridge.background_db,
            linking_biosphere_db=bridge.linking_biosphere_db,
            background_biosphere_db=bridge.background_biosphere_db,
            successors={"code-a-steel": (("code-a-steel-cn-n", 0.25), ("code-a-steel-row", 0.5))},
            flows={},
        )
        sp = self._sp([{"type": "technosphere", "input": (old_db, "code-a-steel"), "amount": 9.0}])

        report = BackgroundRebaser(settings=bridged_settings, bridge=partly).apply(sp)

        amounts = {exc["input"]: exc["amount"] for exc in sp.data[0]["exchanges"]}
        assert amounts == pytest.approx(
            {(new_db, "code-a-steel-cn-n"): 3.0, (new_db, "code-a-steel-row"): 6.0}
        )
        assert report["exchanges_renormalised"] == 1
        assert report["datasets_not_whole"] == 1

    def test_a_product_moves_only_to_a_single_whole_successor(self, bridged_settings, bridge):
        old_db = bridged_settings.linking_release.db_name
        sp = self._sp(
            [
                {"type": "production", "input": (old_db, "code-a-wheat"), "amount": 1.0},
                {"type": "production", "input": (old_db, "code-a-steel"), "amount": 1.0},
            ]
        )
        report = BackgroundRebaser(settings=bridged_settings, bridge=bridge).apply(sp)
        first, second = sp.data[0]["exchanges"]
        assert first["input"] == (bridged_settings.background_release.db_name, "code-a-wheat")
        assert "input" not in second
        assert (report["productions_relinked"], report["productions_lost"]) == (1, 1)

    def test_background_flows_are_renamed_into_the_linking_namespace(self, bridge):
        assert bridge.background_flow_renames() == {
            ("ecoinvent-11-biosphere", "f-co2"): (bridge.linking_biosphere_db, "f-co2"),
            ("ecoinvent-11-biosphere", "f-new"): (bridge.linking_biosphere_db, "f-old"),
        }

    def test_a_missing_bridge_names_the_command_that_builds_it(self, bridged_settings):
        with pytest.raises(ReleaseBridgeMissingError, match="dds-build-release-bridge"):
            ReleaseBridge.load(bridged_settings)


class TestRelease:
    def test_names_follow_brightway(self):
        release = EcoinventRelease("3.13")
        assert release.db_name == "ecoinvent-3.13-cutoff"
        assert release.biosphere_db_name == "ecoinvent-3.13-biosphere"
        assert release.method_prefix == "ecoinvent-3.13"
