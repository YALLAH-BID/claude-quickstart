# Automall daily stock report pipeline

Turns the two daily Excel exports into two deliverables:

| Input (attached each day) | Example name |
|---|---|
| Automall stocklist (AFM.PUR.PRO.1F10 form export) | `STOCKLIST 28.08.2026.xlsx` |
| Stock raw-data workbook (UAE-STOCK / PIPELINE / SOLD DATA sheets) | `28.08.2026 (Stock Raw data).xlsx` |

| Output | Contents |
|---|---|
| `Automall_Stock_Analysis_<date>.xlsx` | Overview KPIs, brand/model months-of-stock, aging by company and make, vehicles aged >90 days, pricing flags, stocklist-vs-raw reconciliation. All figures are live formulas over embedded `DATA_*` sheets. |
| `Stock_Raw_data_<date>_REFRESHED.xlsx` | The raw-data workbook with the Automall rows of `UAE-STOCK` corrected from the stocklist (Buyer, GM, GM%, Prep, Prep %, RSP, Datum, Age, KM). Edits are made directly in the sheet XML so pivot tables, external links, formulas and every other sheet stay byte-for-byte identical. |

## Daily run (what the scheduled session does)

1. Environment (fresh container each day):
   ```bash
   pip install openpyxl
   apt-get install -y libreoffice-calc   # LibreOffice core alone cannot recalculate spreadsheets
   ```
2. Build both outputs:
   ```bash
   python3 stock_report/pipeline.py \
       --stocklist "<today's stocklist>.xlsx" \
       --rawdata   "<today's raw data>.xlsx" \
       --outdir out
   ```
   The script prints a JSON summary (row counts, reconciliation counts, flags,
   refresh cell counts) and exits non-zero if its built-in verification fails —
   in that case do **not** deliver the outputs.
3. Recalculate **only the analysis workbook** with LibreOffice (e.g. the Claude
   xlsx skill's `recalc.py`); it must report `success` with zero errors.
   Do **not** run LibreOffice over the refreshed raw-data workbook — it carries
   external links to network files; it is written with `fullCalcOnLoad` so Excel
   recalculates it on open (use *Refresh All* to update its SUMMARY pivot).
4. Deliver both files to the user with the headline numbers: on-floor stock and
   value, MOS, unpriced / negative-GM counts, vehicles aged >90 days, and the
   top reconciliation issues.

## Assumptions about the exports

- Stocklist: first sheet, headers in row 1, VIN in column C (`Vehicle ID No.`),
  the 49-column A..AW form layout.
- Raw data: sheets named `UAE-STOCK` (28 columns, VIN in B, company in Z),
  `PIPELINE` (VIN in I), and a trailing-3-months sold sheet whose name starts
  with `SOLD DATA (`.
- Amounts in AED; UAE VAT 5%; margin-scheme vehicles carry VAT on the dealer
  margin only, so their VAT-inclusive price equals the net price.
- The refresh deliberately does **not** touch: availability wording (the raw
  file's Available/Customer Tagged convention is kept so its SUMMARY pivot
  layout doesn't shift), vehicle-usage codes (`LV` vs `LCV`), or any
  non-Automall rows. These show up in the RECONCILIATION sheet as
  `Convention` rows instead.

If a sheet or column moves in the source exports, `pipeline.py` fails fast
with a message naming what it could not find — fix the export or update the
column indexes at the top of the script (`RECON_FIELDS`, `REFRESH_TARGETS`).
