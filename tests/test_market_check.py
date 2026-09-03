"""Tests for pricing/market_check.py (stdlib-only, no network)."""

import json
from pathlib import Path

import pytest

from pricing import market_check as mc

EXAMPLE = Path(__file__).resolve().parent.parent / "pricing" / "listings_example.json"


def test_canonical_trim_uses_aliases_and_extra():
    aliases = mc.load_aliases("Mazda", "CX-5")
    assert (
        mc.canonical_trim("Full Option (sports plus)", aliases, [], "Sports Plus")
        == "Sports Plus"
    )
    assert (
        mc.canonical_trim("Carbon Edition", aliases, [], "Sports Plus") == "Sports Plus"
    )
    assert mc.canonical_trim("2.5L I4", aliases, [], "Sports Plus") is None
    assert (
        mc.canonical_trim("2.5L I4", aliases, ["2.5L I4"], "Sports Plus")
        == "Sports Plus"
    )


def test_spec_of():
    assert mc.spec_of("GCC Specs") == "GCC"
    assert mc.spec_of("American Specs") == "Non-GCC"
    assert mc.spec_of(None) == "Unknown"


def test_km_adjust_direction_and_cap():
    # comp has 10k more km than subject -> its price is raised 2%
    assert mc.km_adjust(100_000, 21_712, 11_712, 0.02, 0.10) == pytest.approx(102_000)
    # 100k km gap would be 20%, capped at 10%
    assert mc.km_adjust(100_000, 111_712, 11_712, 0.02, 0.10) == pytest.approx(110_000)


def test_dedupe_merges_cross_listings():
    rows = [
        {"source": "Dubizzle", "km": 12_270, "price": 115_000},
        {"source": "CarSwitch", "km": 12_300, "price": 115_000},
        {"source": "Dubizzle", "km": 12_270, "price": 123_000},
    ]
    out = mc.dedupe(rows, 300, 0.03)
    assert len(out) == 2
    assert out[0]["sources"] == ["CarSwitch", "Dubizzle"]


def test_run_on_example_blends_live_with_autorola(tmp_path):
    out = tmp_path / "r.json"
    rc = mc.main(
        [
            str(EXAMPLE),
            "--make", "Mazda", "--model", "CX-5", "--year", "2025",
            "--km", "11712", "--trim", "Sports Plus",
            "--autorola-median", "118000", "--autorola-n", "4",
            "--json-out", str(out),
        ]
    )  # fmt: skip
    assert rc == 0
    r = json.loads(out.read_text())
    assert r["live_n"] == 3
    assert r["comps_n"] == 7
    assert r["market_ref"] == 117_000
    assert r["confidence"] == "Medium"
    assert r["gap_live_vs_autorola"] == pytest.approx(-0.0243, abs=0.001)
    assert any("Non-GCC" in d for d in r["dropped"])


def test_run_without_autorola_uses_live_median_only(tmp_path):
    out = tmp_path / "r.json"
    mc.main(
        [
            str(EXAMPLE),
            "--make", "Mazda", "--model", "CX-5", "--year", "2025",
            "--km", "11712", "--trim", "Sports Plus", "--json-out", str(out),
        ]
    )  # fmt: skip
    r = json.loads(out.read_text())
    assert r["market_ref"] == 115_000
    assert r["gap_live_vs_autorola"] is None
    assert r["confidence"] == "Medium"


def test_queries_mode_prints_search_set(capsys):
    assert (
        mc.main(
            [
                "--queries",
                "--make",
                "Mazda",
                "--model",
                "CX-5",
                "--year",
                "2025",
                "--trim",
                "Sports Plus",
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "site:uae.dubizzle.com Mazda CX-5 2025 Sports Plus" in text
    assert "Carbon Edition" in text
