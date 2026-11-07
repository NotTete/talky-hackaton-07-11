# PDF layout experiment

**Question.** The hackathon ships ~184k invoice PDFs. Before writing a single business
rule we wanted to know: can we recover *structure* (columns, label→value pairs, field
values) from these files without OCR and without a hand-written parser per company — and
can the layouts themselves tell us the rules (per company, per period)?

**Short answer.** Yes to both. The PDFs have a real text layer, coordinates reconstruct the
table, and a handful of language-independent signals (column roles + label vocabulary +
totals vocabulary) are enough to group documents into templates, infer which label is
`docno` / `date` / `due` / `total` per template, and see the templates change over time.

```
experiments/pdf_layout/
├── extract.py       coordinates → lines → columns → (label, value) pairs
├── tokens.py        token typing: DATE/AMOUNT/DOCNO/IBAN/LABEL/TEXT… + dictionaries
├── fingerprints.py  layout signature + fuzzy similarity + greedy clustering
├── companies.py     per entity/type: templates, field rules, support, drift
├── run.py           the experiment driver (writes out/)
└── out/             docs.jsonl, template_rules.json, cross_entity.json, report.txt
```

```bash
python experiments/pdf_layout/run.py --phase sample            # 2,298 PDFs, ~2 s
python experiments/pdf_layout/run.py --phase sample --phase live --limit 2400
python experiments/pdf_layout/run.py --phase sample --threshold 0.5   # looser families
```

## Method (four steps)

1. **Extract with positions.** `pypdf`'s `visitor_text` gives `(x, y, size, text)` per
   chunk. Chunks sharing a y (tolerance 2.5 pt) form a line; x positions form columns.
   **Table headers are the best anchors**: the line with ≥3 word-ish chunks names the
   columns, so each header chunk becomes a band and the rest is assigned to it. Without a
   header, x-gap clustering (14 pt) is the fallback. Long free-text chunks (footers,
   "this invoice cancels and replaces…") are excluded from clustering — they start in the
   description column and span the page, which otherwise merges every column into one.

2. **Pair labels with values.** Two shapes exist:
   * horizontal — `N°   FA250222794` (two chunks, one baseline);
   * vertical — `Fecha` and `02/06/2025` on consecutive lines.
   The trap: a table row also looks like "text, then numbers". The discriminator is *how
   many* numeric lines follow: a table row has three (`qty, price, amount`), a field has
   one. Currency suffixes (`1 752,08 EUR`) and thousand separators (`,`, `.`, space) are
   normalised in one place (`tokens.RE`).

3. **Type every token.** `DATE | AMOUNT | PCT | QTY | IBAN | BIC | VAT | EMAIL | URL |
   PHONE | DOCNO | CODE | CURRENCY | LABEL | NAME | TEXT | WORD | OTHER`. Identifiers are
   regex-shaped and rare; dictionary words recur across the corpus. Labels get fuzzy-
   canonicalised (`difflib`, 0.82) so `Base Imponible`/`base imponible:` collapse.

4. **Fingerprint and cluster.** A layout is compared on: label vocabulary (containment),
   column role sequence, totals vocabulary, column count, number style. Clustering is
   greedy with a threshold, per *entity × document type* (an invoice and a bank notice are
   not the same layout).

## Findings

### Layout is recoverable, per language

Sampled run on the full `sample` phase (2,298 PDFs, 3 entities, 2021-03…2025-06):

| entity | docs | templates | top template | top 3 templates |
|---|---|---|---|---|
| ES01 | 1,244 | 27 | 49% | **94%** |
| FR01 | 703 | 17 | 48% | **95%** |
| DE01 | 351 | 13 | 54% | **91%** |

Two or three templates explain ~95% of the documents; the long tail is proformas,
bank-account notices and rare variants. Extraction+clustering runs at **~1,100 PDFs/s**
(8 workers), so the whole 184k corpus is ~3 minutes.

Field inference is language-independent (label hints + kind purity + hint priority):

| field | ES01 | FR01 | DE01 | UK01/US01 |
|---|---|---|---|---|
| document no. | `nº` | `n°` | `nr.` | `ref.` |
| issue date | `fecha` | `date` | `datum` | `date` |
| due date | `vencimiento` | `échéance` | `fällig` | `due date` |
| total | `total` | `total ttc` | `gesamt` | `subtotal` |
| order no. | `su pedido` | `votre commande` | `ihre bestellung` | — |

Across all 18 entities seen in `sample` + `live`: `docno` 94%, `total` 94%, `date` 83%,
`due` 78% of companies get a rule; the number style is `decimal comma + dd/mm/yyyy`
everywhere (no `1,234.56` template was found).

### Templates change over time (and the data says when)

* ES01, FR01, DE01 all have **two invoice families**: an older one and a 2024 one
  (`INVOIC-T01` starts 2024-03, `INVOIC-T02` 2024-05) — the same months in which the CSV
  data starts showing extra fields (`po`), i.e. a document-engine change, not noise.
* `live` introduces companies that do not exist in `sample` (AU01, BR01, CH01, CN01,
  IT01, MA01, MX01, NL01, PL01, PT01, UK01, US01), and **ZA01 appears on 2025-12** — the
  acquired subsidiary from `POLICIES.md` §6, with 2 documents and no field rules yet
  (exactly the "masters change in FINAL" warning).
* Style drift is detected separately (ES01/FR01 switch date format around 2025-09), but
  it is the noisiest signal: a document with no date pair reports `?`, so unknown values
  are excluded before comparing.

### Where the information actually is

* 1,400 sampled PDFs: **all have a text layer**, 0 image-only, 1 page each, ~570 chars.
  **No OCR needed**; extraction is ~4 ms/PDF and ~100 MB of text for the whole corpus.
* Amounts are recoverable (99% of documents), dates 100%, line items re-sum to net in 99%
  of blocks — but only with a *line-based* parse: descriptions contain digits
  (`50 lb`, `2 in`, `1.5 HP`), so "take every number in the table" misaligns the columns.

## What this gives the pipeline

`out/template_rules.json` is directly usable as a PDF-reading rule set:

```json
{"template": "INVOIC-T01", "document_type": "invoice", "first": "2024-03", "last": "2025-06",
 "fields": {
   "docno": {"label": "nº",       "kind": "DOCNO",  "support": 1.0},
   "date":  {"label": "fecha",    "kind": "DATE",   "support": 1.0},
   "due":   {"label": "vencimiento", "kind": "DATE", "support": 1.0},
   "total": {"label": "total",    "kind": "AMOUNT", "support": 1.0}}}
```

Meaning: for an ES01 invoice dated 2024-03…2025-06, look for the value paired with `nº` to
get the document number, etc. `support` is the share of documents where that label+kind
was actually present, so a consumer can degrade gracefully instead of assuming.

The remaining PDF-only facts (instalments, retention, "anula y sustituye", service month)
are free text, not fields; the same extraction gives them as `TEXT` lines to the right of
the totals block, which is where the policy clauses live.

## Limitations (honest list)

* **Clustering is heuristic.** Containment similarity keeps supersets in a family;
  Jaccard fragmented one template into dozens. Greedy assignment is order-dependent
  (documents are sorted by period first, which at least makes drift readable). Threshold
  0.62 is tuned on the sample, not learned.
* **False pairs exist**: an address line followed by a VAT id looks like `label → value`
  (`Montclair-sur-Lys → FR008821170`). Filtering by label recurrence removes most; the
  rest show up as one-off labels in the tail templates.
* **Tail templates are real but uninteresting** (proforma, bank notice, credit note).
  Reporting per `document_type` keeps them out of the invoice rules, but the template
  count per entity is inflated by them.
* **No OCR, no PDFs of scans**: if FINAL ships a scanned document, `extract_text` returns
  ~0 chars. A guard (`< 40 chars → flag`) is cheap and should be added before using this
  in the pipeline.
* **Language coverage** is the three sample languages plus English from `live`; a new
  language in FINAL would need a hint row in `companies.FIELD_HINTS` (the structure part
  is language-independent).

## Next steps if this graduates from experiment

1. Cache `out/docs.jsonl` per phase (one pass, ~3 min for the whole corpus) and read it
   from `matching.py` instead of parsing PDFs per run.
2. Feed `fields` into `rules.exception_for` as *evidence*: instalments (`PARCIAL`),
   retention, advance deduction, replaced invoice, service month.
3. Add `pdf_facts.parquet` keyed by `document_number` + `entity`, with the template id and
   the extracted `docno/date/due/total` for cross-checking the CSV master (a document
   whose PDF total disagrees with `ar_invoices.total_amount` is a data-quality signal).
4. Re-run per quarter to keep the drift table current; treat a new template with ≥ 20
   documents as an alert rather than a curiosity.
