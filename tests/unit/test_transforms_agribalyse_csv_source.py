"""``AgribalyseCsvSource`` — full export when present, ecoinvent-free otherwise."""

from __future__ import annotations

import lzma
import os
from dataclasses import replace

import pytest

from transforms import AgribalyseCsvMissingError, AgribalyseCsvSource

PAYLOAD = b"{SimaPro 9.6.0.1}\r\nProcess\r\n\r\nEnd\r\n"


def _write_xz(settings, payload: bytes = PAYLOAD) -> None:
    settings.paths.agribalyse_csv_ecoinvent_free_xz.write_bytes(lzma.compress(payload))


class TestVariant:
    def test_full_csv_wins_when_present(self, settings):
        settings.paths.agribalyse_csv.write_bytes(b"full")
        _write_xz(settings)

        source = AgribalyseCsvSource(settings=settings)

        assert source.variant == "full"
        assert source.path() == settings.paths.agribalyse_csv

    def test_falls_back_to_the_ecoinvent_free_file(self, settings):
        _write_xz(settings)

        source = AgribalyseCsvSource(settings=settings)

        assert source.variant == "ecoinvent-free"
        assert source.path() == settings.paths.agribalyse_csv_ecoinvent_free_cache
        assert source.path().read_bytes() == PAYLOAD

    def test_neither_file_raises_with_both_names(self, settings):
        source = AgribalyseCsvSource(settings=settings)

        assert source.variant is None
        with pytest.raises(AgribalyseCsvMissingError, match=r"AGB32_ecoinvent_free\.CSV\.xz"):
            source.path()


class TestDecompression:
    def test_reuses_the_decompressed_copy(self, settings):
        _write_xz(settings)
        source = AgribalyseCsvSource(settings=settings)
        first = source.path().stat().st_mtime_ns

        assert source.path().stat().st_mtime_ns == first

    def test_refreshes_when_the_xz_is_newer(self, settings):
        _write_xz(settings)
        source = AgribalyseCsvSource(settings=settings)
        cached = source.path()
        _write_xz(settings, PAYLOAD + b"more\r\n")
        newer = cached.stat().st_mtime + 10
        os.utime(settings.paths.agribalyse_csv_ecoinvent_free_xz, (newer, newer))

        assert source.path().read_bytes() == PAYLOAD + b"more\r\n"

    def test_leaves_no_partial_file(self, settings):
        _write_xz(settings)

        AgribalyseCsvSource(settings=settings).path()

        assert [p.name for p in settings.paths.cache.iterdir()] == ["AGB32_ecoinvent_free.CSV"]


class TestDescribe:
    def test_describe_names_variant_and_file(self, settings):
        _write_xz(settings)

        assert AgribalyseCsvSource(settings=settings).describe() == {
            "variant": "ecoinvent-free",
            "file": "AGB32_ecoinvent_free.CSV.xz",
        }


def _block(identifier: str, name: str) -> bytes:
    text = (
        "Process\r\n\r\n"
        f"Process identifier\r\n{identifier}\r\n\r\n"
        "Type\r\nUnit process\r\n\r\n"
        f"Products\r\n{name};kg;1;100;not defined;Cat;\r\n\r\n"
        "End\r\n\r\n"
    )
    return text.encode("latin-1")


OWN = _block("AGB1", "Bread {FR}| baking | Cut-off, U")
COPIED = _block("AGB2", "Steel {RER}| production | Cut-off, S - Copied from Ecoinvent U")
ADAPTED = _block("AGB3", "Banana {CO}| production | Cut-off, U - Adapted from Ecoinvent U")
FULL = b"{SimaPro 9.6.0.1}\r\n\r\n" + OWN + COPIED + ADAPTED


class TestCopiesFreeVariant:
    def test_a_background_release_reads_the_full_export_without_its_copies(self, settings):
        settings.paths.agribalyse_csv.write_bytes(FULL)
        source = AgribalyseCsvSource(settings=settings.with_background("3.13"))

        assert source.variant == "copies-free"
        assert source.rebuilds_unit_copies
        assert source.path() == settings.paths.agribalyse_csv_copies_free_cache
        stripped = source.path().read_bytes()
        assert COPIED not in stripped
        assert OWN in stripped and ADAPTED in stripped
        assert source.describe() == {"variant": "copies-free", "file": "AGB32_final.CSV"}

    def test_without_a_background_release_the_full_export_is_read_as_it_is(self, settings):
        settings.paths.agribalyse_csv.write_bytes(FULL)
        source = AgribalyseCsvSource(settings=settings)

        assert source.variant == "full"
        assert not source.rebuilds_unit_copies
        assert source.parse_cache == settings.paths.importer_cache_pkl

    def test_the_choice_can_be_made_either_way_whatever_the_background(self, settings):
        settings.paths.agribalyse_csv.write_bytes(FULL)

        replaced = AgribalyseCsvSource(settings=replace(settings, embedded_copies="replace"))
        kept = AgribalyseCsvSource(
            settings=replace(settings.with_background("3.13"), embedded_copies="keep")
        )

        assert replaced.variant == "copies-free"
        assert kept.variant == "full"

    def test_each_reading_of_the_full_export_has_its_own_parse_cache(self, settings):
        settings.paths.agribalyse_csv.write_bytes(FULL)
        source = AgribalyseCsvSource(settings=replace(settings, embedded_copies="replace"))

        assert source.parse_cache == settings.paths.importer_cache_copies_free_pkl
        assert source.parse_cache != settings.paths.importer_cache_pkl

    def test_the_stripped_copy_is_rebuilt_when_the_full_export_is_newer(self, settings):
        settings.paths.agribalyse_csv.write_bytes(FULL)
        source = AgribalyseCsvSource(settings=replace(settings, embedded_copies="replace"))
        cached = source.path()
        settings.paths.agribalyse_csv.write_bytes(FULL + OWN.replace(b"AGB1", b"AGB9"))
        newer = cached.stat().st_mtime + 10
        os.utime(settings.paths.agribalyse_csv, (newer, newer))

        assert b"AGB9" in source.path().read_bytes()

    def test_the_committed_file_is_unaffected(self, settings):
        _write_xz(settings)
        source = AgribalyseCsvSource(settings=replace(settings, embedded_copies="replace"))

        assert source.variant == "ecoinvent-free"

    def test_an_unknown_choice_is_refused(self, settings):
        settings.paths.agribalyse_csv.write_bytes(FULL)
        source = AgribalyseCsvSource(settings=replace(settings, embedded_copies="maybe"))

        with pytest.raises(ValueError, match="DDS_EMBEDDED_COPIES"):
            _ = source.variant
