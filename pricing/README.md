# Live market check (appraisal step)

Repeatable procedure for checking a used-car appraisal against live UAE
listings, so every enquiry gets the same search, the same filters and the same
arithmetic. Output is one `market_ref` figure for the appraisal calculator plus
a comparison with the Autorola median.

**Privacy rule.** Only make, model, year and trim ever go out to a search
engine. Chassis, plate, customer details, SAP or Autorola exports never leave
the company. Listings come *in*; nothing goes *out*.

## Files

| File | Purpose |
|---|---|
| `market_check.py` | Clean, normalise, de-duplicate and blend listings into `market_ref`. Standard library only, no network. |
| `trim_aliases.json` | Per make/model: canonical GCC trim → other names sellers use for it. Grows with every appraisal. |
| `listings_example.json` | Real listings captured 03.09.2026 for a 2025 Mazda CX-5 Sports Plus. Shows the input shape. |

## The procedure (six steps, every time)

1. **Confirm the GCC trim name first.** Photos and Autorola labels are often
   wrong (Autorola said "Luxury", the car was a "Sports Plus"). Check the
   brand's UAE grades page and match the giveaway features: wheel colour and
   size, interior colour, sunroof, 360 camera, HUD, power tailgate. Add any new
   alias to `trim_aliases.json`.
2. **Print the search set** and run every query:
   ```bash
   python pricing/market_check.py --queries --make Mazda --model CX-5 --year 2025 --trim "Sports Plus"
   ```
   Sites, in order of how much their prices count: Dubizzle, DubiCars,
   YallaMotor, CarSwitch, Kavak, brand pre-owned site, then one query for the
   new price.
3. **Capture every same-model listing** into a JSON list, one object per
   listing: `source, year, trim (as written), km, price, spec, seller`. Do not
   pre-filter by hand; the script does it and shows what it excluded so the
   exclusions are auditable.
4. **Run the check** with the Autorola numbers:
   ```bash
   python pricing/market_check.py listings.json --make Mazda --model CX-5 \
       --year 2025 --km 11712 --trim "Sports Plus" \
       --autorola-median 118000 --autorola-n 4 --autorola-low 112000 --autorola-high 124000
   ```
5. **Read the gap line.** Live vs Autorola within ±8% means the two sources
   agree; use `MARKET_REF`. Beyond ±8% the script prints `investigate`:
   check comp dates, trim mismatch, or a facelift cliff before pricing.
6. **Feed `MARKET_REF` to the calculator** and put the "Live vs Autorola" line
   and the comp count in the appraisal log notes.

## What the script does, in order

| Step | Rule | Flag to change it |
|---|---|---|
| Trim match | Listing text must contain the canonical trim or one of its aliases; unknown trims are excluded and listed | `--alias` (repeatable), `trim_aliases.json` |
| Spec | GCC only. "American / Japanese / Korean / European" excluded; unknown excluded unless told otherwise | `--keep-unknown-spec` |
| Year | Exact model year. `--year-tolerance 1` allows ±1 year, adjusted by the annual depreciation | `--year-tolerance`, `--annual-drop` (default 12%) |
| De-duplicate | Same km (±300) and same price (±3%) = one car listed twice; sources merged | `--dedupe-km`, `--dedupe-price` |
| Mileage normalise | Each comp restated at the subject's km: 2% per 10,000 km, capped at ±10% | `--km-rate`, `--km-cap` |
| Outliers | With 3+ comps, anything more than 15% from the median is dropped and listed | `--outlier` |
| Transaction estimate | Live median minus 4% negotiation room, shown for reference | `--haircut` |
| Blend | `market_ref` = live median and Autorola median weighted by their comp counts | `--autorola-n` |
| Confidence | High: 5+ live, 5+ Autorola, gap under 5%. Medium: 3+ live or 4+ Autorola. Low otherwise | — |

Percentages mirror `pricing_rules.json` in the appraisal skill (mileage 2% per
10,000 km). Change them in one place and pass the same value here.

## Why this is the "AI-level" version of the search

- Same query set every time, so two appraisers get the same evidence.
- Trim aliases are learned once and reused; a "Full Option" private listing and
  a "Carbon Edition" dealer listing both resolve to "Sports Plus".
- Comps are compared at the subject's mileage, not raw.
- Cross-listings on two sites count once.
- Every exclusion is printed, so the figure can be defended to a manager.
- The JSON output (`--json-out`) is the training row for the future pricing
  engine: subject, comps, adjustments and the final reference.

## Worked example

`listings_example.json` holds ten listings captured for a 2025 CX-5 Sports
Plus with 11,712 km. The script keeps three (same trim, GCC, 2025), excludes
GT / Trend Plus / Signature / unknown-trim rows, an American-spec car and a
2024 car with no mileage, then blends the live median (AED 115,128) with the
Autorola median (AED 118,000) into `market_ref` AED 117,000, Medium
confidence, gap −2.4%.
