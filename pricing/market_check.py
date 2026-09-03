#!/usr/bin/env python3
"""Live-market check for a used-car appraisal.

Turns listings copied from public UAE classifieds into one market reference
that ``price_calc.py`` can use, and compares it with the Autorola median.

Privacy rule: only make, model, year and trim ever go *out* to a search
engine. Listings come *in*. Chassis, plate, customer and SAP data never
touch this script.

Usage::

    python pricing/market_check.py listings.json --make Mazda --model CX-5 \
        --year 2025 --km 11712 --trim "Sports Plus" \
        --autorola-median 118000 --autorola-n 4 \
        --autorola-low 112000 --autorola-high 124000

    python pricing/market_check.py --queries --make Mazda --model CX-5 \
        --year 2025 --trim "Sports Plus"          # print the search set only

``listings.json`` is a list of objects with keys ``source, year, trim, km,
price, spec`` (``seller``, ``url``, ``note`` optional). Prices in AED.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ALIAS_FILE = HERE / "trim_aliases.json"

# Public UAE sites that list used cars. Order = how much weight their prices
# usually deserve (dealer platforms first, private classifieds last).
SITES = [
    ("Dubizzle", "site:uae.dubizzle.com {make} {model} {year} {trim}"),
    ("DubiCars", "site:dubicars.com {make} {model} {year} used"),
    ("YallaMotor", "site:uae.yallamotor.com used {make} {model} {year}"),
    ("CarSwitch", "site:carswitch.com {make} {model} {year}"),
    ("Kavak", "site:kavak.com/ae {make} {model} {year}"),
    ("Brand pre-owned", "{make} pre-owned UAE {model} {year} {trim}"),
    ("New price", "{make} {model} {year} {trim} UAE new price AED"),
]

GCC_WORDS = ("gcc", "uae", "gulf")
NON_GCC_WORDS = (
    "american",
    "usa",
    "us spec",
    "japan",
    "japanese",
    "canad",
    "korea",
    "europe",
)


def load_aliases(make: str, model: str) -> dict[str, list[str]]:
    """Return {canonical trim: [aliases]} for this make/model, if known."""
    if not ALIAS_FILE.exists():
        return {}
    table = json.loads(ALIAS_FILE.read_text(encoding="utf-8"))
    key = f"{make} {model}".strip().lower()
    for name, trims in table.items():
        if name.lower() == key:
            return trims
    return {}


def canonical_trim(
    raw: str | None, aliases: dict[str, list[str]], extra: list[str], target: str
) -> str | None:
    """Map a listing's trim text to a canonical name, or None if unknown."""
    text = (raw or "").lower()
    if not text:
        return None
    for canon, names in aliases.items():
        for candidate in [canon, *names]:
            if candidate.lower() in text:
                return canon
    for name in extra:
        if name.lower() in text:
            return target
    if target.lower() in text:
        return target
    return None


def spec_of(raw: str | None) -> str:
    text = (raw or "").lower()
    if any(w in text for w in NON_GCC_WORDS):
        return "Non-GCC"
    if any(w in text for w in GCC_WORDS):
        return "GCC"
    return "Unknown"


def km_adjust(
    price: float, comp_km: float, subject_km: float, rate: float, cap: float
) -> float:
    """Restate a comp's price as if it had the subject's mileage."""
    delta = (comp_km - subject_km) / 10_000 * rate
    delta = max(-cap, min(cap, delta))
    return price * (1 + delta)


def dedupe(rows: list[dict], km_tol: int, price_tol: float) -> list[dict]:
    """Merge cross-listings: same km (±km_tol) and price (±price_tol)."""
    kept: list[dict] = []
    for row in rows:
        twin = next(
            (
                k
                for k in kept
                if abs(k["km"] - row["km"]) <= km_tol
                and abs(k["price"] - row["price"]) / k["price"] <= price_tol
            ),
            None,
        )
        if twin:
            twin["sources"] = sorted(set(twin["sources"]) | {row["source"]})
        else:
            kept.append({**row, "sources": [row["source"]]})
    return kept


def round_to(x: float, step: int) -> int:
    return int(round(x / step) * step)


def confidence(n_live: int, n_auto: int, gap: float | None) -> str:
    if n_live >= 5 and n_auto >= 5 and gap is not None and abs(gap) < 0.05:
        return "High"
    if n_live >= 3 or n_auto >= 4:
        return "Medium"
    return "Low"


def run(args: argparse.Namespace) -> dict:
    aliases = load_aliases(args.make, args.model)
    raw = json.loads(Path(args.listings).read_text(encoding="utf-8"))
    dropped: list[str] = []
    usable: list[dict] = []
    for i, item in enumerate(raw, 1):
        tag = (
            f"#{i} {item.get('source', '?')} {item.get('trim', '?')} "
            f"{item.get('price', '?')}"
        )
        trim = canonical_trim(item.get("trim"), aliases, args.alias, args.trim)
        if trim != args.trim:
            dropped.append(f"{tag}: trim '{item.get('trim')}' -> {trim or 'unknown'}")
            continue
        spec = spec_of(item.get("spec"))
        if spec == "Non-GCC" or (spec == "Unknown" and not args.keep_unknown_spec):
            dropped.append(f"{tag}: spec {spec}")
            continue
        year = item.get("year")
        if year is None or abs(int(year) - args.year) > args.year_tolerance:
            dropped.append(f"{tag}: year {year}")
            continue
        if item.get("km") is None or item.get("price") is None:
            dropped.append(f"{tag}: missing km or price")
            continue
        usable.append(
            {
                "source": item.get("source", "?"),
                "year": int(year),
                "km": float(item["km"]),
                "price": float(item["price"]),
                "seller": item.get("seller", ""),
            }
        )

    comps = dedupe(usable, args.dedupe_km, args.dedupe_price)
    for c in comps:
        adj = km_adjust(c["price"], c["km"], args.km, args.km_rate, args.km_cap)
        year_gap = args.year - c["year"]  # +1 = comp is one year older
        adj *= (1 + args.annual_drop) ** year_gap
        c["adjusted"] = adj

    outliers: list[dict] = []
    if len(comps) >= 3:
        med = statistics.median(c["adjusted"] for c in comps)
        keep = []
        for c in comps:
            if abs(c["adjusted"] - med) / med > args.outlier:
                outliers.append(c)
            else:
                keep.append(c)
        comps = keep

    result: dict = {
        "subject": (
            f"{args.year} {args.make} {args.model} {args.trim}, {args.km:,.0f} km"
        ),
        "live_n": len(comps),
        "dropped": dropped,
        "outliers": outliers,
        "comps": comps,
    }
    if comps:
        vals = [c["adjusted"] for c in comps]
        live_median = statistics.median(vals)
        result.update(
            live_median=live_median,
            live_mean=statistics.mean(vals),
            live_low=min(vals),
            live_high=max(vals),
            live_transaction=live_median * (1 - args.haircut),
        )
    n_auto = args.autorola_n or 0
    if args.autorola_median and comps:
        w_live = len(comps) / (len(comps) + n_auto) if n_auto else 1.0
        blended = w_live * result["live_median"] + (1 - w_live) * args.autorola_median
        result["gap_live_vs_autorola"] = (
            result["live_median"] - args.autorola_median
        ) / args.autorola_median
        result["weight_live"] = w_live
    elif args.autorola_median:
        blended = args.autorola_median
        result["gap_live_vs_autorola"] = None
    elif comps:
        blended = result["live_median"]
        result["gap_live_vs_autorola"] = None
    else:
        blended = None
        result["gap_live_vs_autorola"] = None
    result["market_ref"] = round_to(blended, args.round) if blended else None
    result["confidence"] = confidence(
        len(comps), n_auto, result["gap_live_vs_autorola"]
    )
    result["comps_n"] = len(comps) + n_auto
    return result


def print_report(r: dict, args: argparse.Namespace) -> None:
    aed = "AED {:>10,.0f}"
    print("MARKET CHECK")
    print(f"Subject        : {r['subject']}")
    if args.autorola_median:
        rng = ""
        if args.autorola_low and args.autorola_high:
            rng = f", range {args.autorola_low:,.0f}-{args.autorola_high:,.0f}"
        print(
            f"Autorola       : median {args.autorola_median:,.0f} "
            f"(n={args.autorola_n or 0}{rng})"
        )
    print(
        f"Live comps     : n={r['live_n']} (same trim, GCC, "
        f"year ±{args.year_tolerance}, km-normalised)"
    )
    for c in r["comps"]:
        src = "/".join(c["sources"])
        print(
            f"  {src:<22} {c['year']} {c['km']:>7,.0f} km  asking {c['price']:>9,.0f}"
            f"  -> at {args.km:,.0f} km {c['adjusted']:>9,.0f}"
        )
    if r["live_n"]:
        print(
            f"Live median    : {aed.format(r['live_median'])}   "
            f"(low {r['live_low']:,.0f}, high {r['live_high']:,.0f})"
        )
        print(
            f"Live transact. : {aed.format(r['live_transaction'])}   "
            f"(asking minus {args.haircut:.0%} negotiation)"
        )
    gap = r.get("gap_live_vs_autorola")
    if gap is not None:
        flag = "  <-- investigate" if abs(gap) > 0.08 else ""
        print(
            f"Live vs Autorola: {gap:+.1%}{flag}   "
            f"(blend weight live {r['weight_live']:.0%})"
        )
    if r["market_ref"]:
        print(
            f"MARKET_REF     : {aed.format(r['market_ref'])}   -> price_calc.py "
            f'--json \'{{"market_ref":{r["market_ref"]},"comps_n":{r["comps_n"]}}}\''
        )
    else:
        print("MARKET_REF     : none - no usable comps and no Autorola median")
    print(f"Confidence     : {r['confidence']}")
    if r["outliers"]:
        print("Outliers dropped:")
        for c in r["outliers"]:
            print(
                f"  {'/'.join(c['sources'])} {c['km']:,.0f} km "
                f"{c['price']:,.0f} (adj {c['adjusted']:,.0f})"
            )
    if r["dropped"]:
        print("Excluded listings:")
        for d in r["dropped"]:
            print(f"  {d}")


def print_queries(args: argparse.Namespace) -> None:
    print(f"SEARCH SET for {args.year} {args.make} {args.model} {args.trim}")
    print(
        "(only make/model/year/trim leave the company - "
        "never chassis, plate or customer data)"
    )
    for name, template in SITES:
        print(
            f"  {name:<16} "
            + template.format(
                make=args.make, model=args.model, year=args.year, trim=args.trim
            )
        )
    aliases = load_aliases(args.make, args.model)
    if aliases:
        print("Known trim aliases for this model:")
        for canon, names in aliases.items():
            print(f"  {canon:<14} = {', '.join(names) if names else '-'}")
    print(
        "Capture per listing: source, year, trim (as written), km, price, spec, seller."
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("listings", nargs="?", help="JSON file of listings")
    ap.add_argument(
        "--queries", action="store_true", help="print the search set and exit"
    )
    ap.add_argument("--make", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--year", type=int, required=True)
    ap.add_argument(
        "--trim", required=True, help="canonical GCC trim name of the subject"
    )
    ap.add_argument(
        "--alias",
        action="append",
        default=[],
        help="extra text that means the same trim (repeatable)",
    )
    ap.add_argument("--km", type=float, default=0.0, help="subject odometer")
    ap.add_argument("--autorola-median", type=float)
    ap.add_argument("--autorola-n", type=int, default=0)
    ap.add_argument("--autorola-low", type=float)
    ap.add_argument("--autorola-high", type=float)
    ap.add_argument(
        "--haircut",
        type=float,
        default=0.04,
        help="asking -> transaction discount (default 4%%)",
    )
    ap.add_argument(
        "--km-rate",
        type=float,
        default=0.02,
        help="price change per 10,000 km (default 2%%)",
    )
    ap.add_argument(
        "--km-cap", type=float, default=0.10, help="max mileage adjustment either way"
    )
    ap.add_argument(
        "--annual-drop",
        type=float,
        default=0.12,
        help="per-year depreciation for adjacent-year comps",
    )
    ap.add_argument("--year-tolerance", type=int, default=0)
    ap.add_argument(
        "--outlier",
        type=float,
        default=0.15,
        help="drop comps further than this from the median",
    )
    ap.add_argument("--dedupe-km", type=int, default=300)
    ap.add_argument("--dedupe-price", type=float, default=0.03)
    ap.add_argument("--keep-unknown-spec", action="store_true")
    ap.add_argument("--round", type=int, default=500)
    ap.add_argument("--json-out", help="write the full result as JSON here")
    args = ap.parse_args(argv)

    if args.queries:
        print_queries(args)
        return 0
    if not args.listings:
        ap.error("listings file required unless --queries")
    if not args.km:
        ap.error("--km is required to normalise comps")
    result = run(args)
    print_report(result, args)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
