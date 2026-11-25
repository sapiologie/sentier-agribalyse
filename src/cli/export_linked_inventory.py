"""``dds-export-linked-inventory`` — the linked Agribalyse inventory as one JSON file.

Writes what each Agribalyse process takes per unit of its reference
product: other processes, ecoinvent datasets by their published ids, and
ecoinvent elementary flows. Meant for a store that already holds ecoinvent
and sums the scores itself. Reads the matrices of the last ``dds-link-all``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import ClassVar

from cli._base import BaseCli
from exports.linked_inventory import LinkedInventoryExporter
from scoring.scoring_package_locator import ScoringPackageLocator


class ExportLinkedInventoryCli(BaseCli):
    PROG: ClassVar[str] = "cli.export_linked_inventory"
    DESCRIPTION: ClassVar[str] = (
        "Export the linked Agribalyse inventory of the last link run as JSON: per process, "
        "its inputs from other processes, ecoinvent datasets and ecoinvent elementary flows."
    )

    @classmethod
    def parser(cls) -> argparse.ArgumentParser:
        p = super().parser()
        p.add_argument(
            "--output",
            type=Path,
            default=None,
            help=(
                "File to write. Default: cache/linked_inventory/links_ecoinvent_<version>.json. "
                "The file holds licensed ecoinvent data: keep it out of any shared place."
            ),
        )
        return p

    def execute(self, args: argparse.Namespace) -> None:
        s = self.settings
        target = args.output or (
            s.paths.cache
            / "linked_inventory"
            / f"links_ecoinvent_{s.background_release.version}.json"
        )
        package = ScoringPackageLocator(settings=s).load()
        report = LinkedInventoryExporter(settings=s, package=package).export(target)
        print(json.dumps(report, indent=2))
        print("This file is licensed ecoinvent data: never commit or publish it.")


def main() -> int:
    return ExportLinkedInventoryCli().run()


if __name__ == "__main__":
    sys.exit(main())
