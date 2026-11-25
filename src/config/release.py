"""``EcoinventRelease`` — one ecoinvent release and the names Brightway gives it.

Two releases can be in play at once. The *linking* release is the one the
Agribalyse export names its inputs in (3.9.1 for Agribalyse 3.2); every
matching rule of the pipeline speaks its vocabulary. The *background*
release is the one the inventory is built and scored on. They are the same
unless ``Settings.background_ecoinvent_version`` says otherwise.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EcoinventRelease:
    """A release version and system model, with the database names
    ``bw2io.import_ecoinvent_release`` derives from them."""

    version: str
    system_model: str = "cutoff"

    @property
    def db_name(self) -> str:
        return f"ecoinvent-{self.version}-{self.system_model}"

    @property
    def biosphere_db_name(self) -> str:
        return f"ecoinvent-{self.version}-biosphere"

    @property
    def method_prefix(self) -> str:
        """First element of the release's LCIA method tuples."""
        return f"ecoinvent-{self.version}"
