"""``AgribalyseCsvSource`` — which SimaPro CSV the pipeline reads.

Two variants of the Agribalyse 3.2 export exist:

* ``full`` — ``source/AGB32_final.CSV``, ADEME's export with the embedded
  ecoinvent copies. Local-only, never committed.
* ``ecoinvent-free`` — ``source/AGB32_ecoinvent_free.CSV.xz``, the same
  export without the ``Copied from Ecoinvent`` and ``Adapted from
  Ecoinvent`` blocks (see
  ``EcoinventStripper``). Committed, so a clone runs with only an
  ecoinvent licence.

The full export wins when it is present, so results of existing local
set-ups do not move. Otherwise the committed file is decompressed once
into ``cache/``.

A third variant is derived from the full export on demand:

* ``copies-free`` — the full export without its ``Copied from Ecoinvent``
  blocks, ADEME's ``Adapted from Ecoinvent`` datasets kept. The copies are
  ecoinvent datasets frozen as the linking release had them; without them
  the pipeline takes those datasets from the user's ecoinvent, as it does
  for the committed file, while ADEME's adaptations stay ADEME's. It is
  read when ``Settings.replaces_embedded_copies`` holds, which a background
  release implies: a background release cannot reach an embedded copy.
"""

from __future__ import annotations

import lzma
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from config import Settings
from core.logging import Logging
from transforms.ecoinvent_stripper import EcoinventStripper


class AgribalyseCsvMissingError(FileNotFoundError):
    """Neither variant of the Agribalyse CSV is on disk."""


@dataclass(frozen=True)
class AgribalyseCsvSource:
    settings: Settings

    FULL: ClassVar[str] = "full"
    ECOINVENT_FREE: ClassVar[str] = "ecoinvent-free"
    COPIES_FREE: ClassVar[str] = "copies-free"

    @property
    def _log(self) -> Any:
        return Logging.get(__name__)

    @property
    def variant(self) -> str | None:
        paths = self.settings.paths
        if paths.agribalyse_csv.exists():
            return self.COPIES_FREE if self.settings.replaces_embedded_copies else self.FULL
        if paths.agribalyse_csv_ecoinvent_free_xz.exists():
            return self.ECOINVENT_FREE
        return None

    @property
    def rebuilds_unit_copies(self) -> bool:
        """True when the file read holds no ``Copied from Ecoinvent`` block, so
        the copied unit processes are to be rebuilt from the user's ecoinvent."""
        return self.variant in (self.ECOINVENT_FREE, self.COPIES_FREE)

    @property
    def parse_cache(self) -> Path:
        """Pickle of the parsed file. The copies-free variant has its own, so
        that switching between it and the full export never serves a stale parse."""
        paths = self.settings.paths
        if self.variant == self.COPIES_FREE:
            return paths.importer_cache_copies_free_pkl
        return paths.importer_cache_pkl

    def path(self) -> Path:
        """The CSV to parse. Decompresses the committed file when needed."""
        paths = self.settings.paths
        variant = self.variant
        if variant == self.FULL:
            return paths.agribalyse_csv
        if variant == self.COPIES_FREE:
            return self._copies_free()
        if variant == self.ECOINVENT_FREE:
            return self._decompressed()
        raise AgribalyseCsvMissingError(
            f"No Agribalyse CSV found: expected {paths.agribalyse_csv_ecoinvent_free_xz.name} "
            f"(shipped with the repository) or {paths.agribalyse_csv.name} (ADEME's full "
            f"export) in {paths.source}."
        )

    def describe(self) -> dict[str, str | None]:
        """Run-report payload: which variant and which file fed the run."""
        paths = self.settings.paths
        variant = self.variant
        names = {
            self.FULL: paths.agribalyse_csv.name,
            self.COPIES_FREE: paths.agribalyse_csv.name,
            self.ECOINVENT_FREE: paths.agribalyse_csv_ecoinvent_free_xz.name,
        }
        return {"variant": variant, "file": names.get(variant) if variant else None}

    def _copies_free(self) -> Path:
        """The full export without its ``Copied from Ecoinvent`` blocks, built once."""
        paths = self.settings.paths
        full = paths.agribalyse_csv
        out = paths.agribalyse_csv_copies_free_cache
        if out.exists() and out.stat().st_mtime >= full.stat().st_mtime:
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        result = EcoinventStripper(markers=(EcoinventStripper.COPIED,)).strip(full.read_bytes())
        partial = out.with_suffix(out.suffix + ".partial")
        try:
            partial.write_bytes(result.stripped)
            os.replace(partial, out)
        finally:
            partial.unlink(missing_ok=True)
        self._log.info(
            "agribalyse_csv.copies_stripped",
            path=out.name,
            removed=len(result.removed),
            kept=result.processes_kept,
            mb=round(out.stat().st_size / 1e6, 1),
        )
        return out

    def _decompressed(self) -> Path:
        paths = self.settings.paths
        xz = paths.agribalyse_csv_ecoinvent_free_xz
        out = paths.agribalyse_csv_ecoinvent_free_cache
        if out.exists() and out.stat().st_mtime >= xz.stat().st_mtime:
            return out
        out.parent.mkdir(parents=True, exist_ok=True)
        partial = out.with_suffix(out.suffix + ".partial")
        try:
            with lzma.open(xz, "rb") as src, partial.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(partial, out)
        finally:
            partial.unlink(missing_ok=True)
        self._log.info(
            "agribalyse_csv.decompressed", path=out.name, mb=round(out.stat().st_size / 1e6, 1)
        )
        return out
