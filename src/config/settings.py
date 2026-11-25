"""Pipeline-level settings. Frozen dataclass; pass to every component."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import ClassVar

from config.paths import Paths
from config.release import EcoinventRelease


@dataclass(frozen=True)
class Settings:
    """End-to-end pipeline configuration. Immutable; use ``with_*`` to evolve."""

    DEFAULT_ECOINVENT_FOR_AGRIBALYSE: ClassVar[Mapping[str, str]] = MappingProxyType(
        {"3.2": "3.9.1", "4.0-beta": "3.11"}
    )

    BACKGROUND_ENV: ClassVar[str] = "DDS_BACKGROUND_ECOINVENT"
    """Environment variable (or ``.env`` line) naming the background release."""
    EMBEDDED_COPIES_ENV: ClassVar[str] = "DDS_EMBEDDED_COPIES"
    """Environment variable (or ``.env`` line) setting ``embedded_copies``."""
    EMBEDDED_COPIES: ClassVar[tuple[str, ...]] = ("keep", "replace")

    agribalyse_version: str = "3.2"
    ecoinvent_version: str | None = None
    background_ecoinvent_version: str | None = None
    """ecoinvent release the inventory is built and scored on, when it is not
    the release Agribalyse was linked against. ``None``: the linking release
    is the background too, and the pipeline behaves as it always did."""
    embedded_copies: str | None = None
    """What to do with the ecoinvent datasets ADEME's full export embeds as they
    are (``Copied from Ecoinvent``): ``"keep"`` scores them as the export holds
    them, ``"replace"`` takes them from the user's ecoinvent instead, as the
    shipped ecoinvent-free file does. ``None``: ``"replace"`` with a background
    release, since the embedded copies are frozen on the linking release, and
    ``"keep"`` without one."""
    ecoinvent_system_model: str = "cutoff"
    ef_version: str = "3.1"

    project_name: str | None = None
    biosphere_db_name: str = "ecoinvent-3.9.1-biosphere"
    agribalyse_db_name: str | None = None
    ef_db_name: str = "ef"

    apply_llm_overrides: bool = True
    """``--no-llm`` toggles this. Gates BOTH LLM overrides (tier 10) AND the
    curated synonym fallback (tier 11) — fix 1.j."""

    apply_transitive_layer: bool = False
    """The 'ecoinvent flows - EF v3.1 map' transitive sheet (806 rows). Off by default."""

    paths: Paths = field(default_factory=Paths)

    @classmethod
    def default_ecoinvent_for(cls, agribalyse_version: str) -> str:
        try:
            return cls.DEFAULT_ECOINVENT_FOR_AGRIBALYSE[agribalyse_version]
        except KeyError as exc:
            supported = ", ".join(sorted(cls.DEFAULT_ECOINVENT_FOR_AGRIBALYSE))
            raise ValueError(
                f"No ADEME-native ecoinvent pairing registered for Agribalyse "
                f"{agribalyse_version!r}. Known versions: {supported}."
            ) from exc

    @property
    def resolved_ecoinvent_version(self) -> str:
        return self.ecoinvent_version or self.default_ecoinvent_for(self.agribalyse_version)

    @property
    def resolved_project_name(self) -> str:
        return self.project_name or f"agribalyse-{self.agribalyse_version}-sentier"

    @property
    def resolved_agribalyse_db_name(self) -> str:
        return self.agribalyse_db_name or f"agribalyse-{self.agribalyse_version}"

    @property
    def ecoinvent_db_name(self) -> str:
        return f"ecoinvent-{self.resolved_ecoinvent_version}-{self.ecoinvent_system_model}"

    @property
    def linking_release(self) -> EcoinventRelease:
        """The release the Agribalyse export names its inputs in."""
        return EcoinventRelease(self.resolved_ecoinvent_version, self.ecoinvent_system_model)

    @property
    def background_release(self) -> EcoinventRelease:
        """The release the inventory is built and scored on."""
        return EcoinventRelease(
            self.background_ecoinvent_version or self.resolved_ecoinvent_version,
            self.ecoinvent_system_model,
        )

    @property
    def has_background_release(self) -> bool:
        """True when the background release differs from the linking release."""
        return self.background_release != self.linking_release

    def ef_methods_snapshot_for(self, release: EcoinventRelease) -> Path:
        """EF-method snapshot of ``release``. The linking release keeps the
        historical unversioned file name."""
        if release == self.linking_release:
            return self.paths.ef_methods_snapshot_json
        return self.paths.release_ef_methods_json(release)

    @property
    def replaces_embedded_copies(self) -> bool:
        """True when the full export's ``Copied from Ecoinvent`` blocks are to be
        taken from the user's ecoinvent rather than scored as embedded."""
        if self.embedded_copies is None:
            return self.has_background_release
        if self.embedded_copies not in self.EMBEDDED_COPIES:
            raise ValueError(
                f"{self.EMBEDDED_COPIES_ENV} is {self.embedded_copies!r}: "
                f"expected one of {', '.join(self.EMBEDDED_COPIES)}."
            )
        return self.embedded_copies == "replace"

    @classmethod
    def from_environment(cls) -> Settings:
        """Default settings, with the background release ``DDS_BACKGROUND_ECOINVENT``
        names and the ``DDS_EMBEDDED_COPIES`` choice."""
        return replace(
            cls(),
            background_ecoinvent_version=os.environ.get(cls.BACKGROUND_ENV) or None,
            embedded_copies=os.environ.get(cls.EMBEDDED_COPIES_ENV) or None,
        )

    def with_ecoinvent(self, version: str | None) -> Settings:
        return replace(self, ecoinvent_version=version)

    def with_background(self, version: str | None) -> Settings:
        return replace(self, background_ecoinvent_version=version)

    def with_no_llm(self) -> Settings:
        return replace(self, apply_llm_overrides=False)
