"""``dds-build-release-bridge`` — bridge the linking release to the background release.

Reads the correspondence workbooks ecoinvent publishes for each release
step (ecoQuery > Files > Supporting documents) and the snapshots of both
releases, and writes ``registry/release_bridge_activities.parquet`` and
``registry/release_bridge_flows.parquet``. Run it once both releases are
bootstrapped and ``DDS_BACKGROUND_ECOINVENT`` is set.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import ClassVar

from bootstrap.release_bridge import ReleaseBridgeBuilder
from cli._base import BaseCli


class BuildReleaseBridgeCli(BaseCli):
    PROG: ClassVar[str] = "cli.build_release_bridge"
    DESCRIPTION: ClassVar[str] = (
        "Build the dataset and elementary-flow bridge from the ecoinvent release Agribalyse "
        "is linked against to the background release, from ecoinvent's correspondence files."
    )

    @classmethod
    def parser(cls) -> argparse.ArgumentParser:
        p = super().parser()
        p.add_argument(
            "--correspondence-dir",
            type=Path,
            required=True,
            help=(
                "Directory holding ecoinvent's 'Correspondence File vX - vY.xlsx' workbooks, "
                "one per release step between the two releases."
            ),
        )
        return p

    def execute(self, args: argparse.Namespace) -> None:
        report = ReleaseBridgeBuilder(
            settings=self.settings, correspondence_dir=args.correspondence_dir
        ).build()
        print(json.dumps(report, indent=2))


def main() -> int:
    return BuildReleaseBridgeCli().run()


if __name__ == "__main__":
    sys.exit(main())
