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
├── lang.py          language ID (4 methods + fusion) and document-quality flags
├── run.py           layout experiment driver (writes out/)
├── run_lang.py      language benchmark + quality census (writes out/lang_*.json)
└── out/             docs.jsonl, template_rules.json, cross_entity.json, report.txt
```

```bash
python experiments/pdf_layout/run.py --phase sample            # 2,298 PDFs, ~2 s
python experiments/pdf_layout/run.py --phase sample --phase live --limit 2400
python experiments/pdf_layout/run.py --phase sample --threshold 0.5   # looser families

python experiments/pdf_layout/run_lang.py                     # language benchmark (~2k docs)
python experiments/pdf_layout/run_lang.py --all-live           # census over all 19k PDFs
python experiments/pdf_layout/run_lang.py --all-live --census-only   # flags + language mix only
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


## Language and document quality

`run_lang.py` answers two questions the layout work depends on: **what language is this
page** (the entity's country is not the answer: CN01 invoices in English, MA01 in French)
and **is the page long enough to parse at all**.

Ground truth is the entity's country from `masters/entities.csv`, restricted to the
unambiguous countries (ES/MX→es, PT/BR→pt, FR, IT, DE, NL, GB/US/AU→en, PL); CH, MA and CN
are multilingual by construction and are reported but not scored. Split is 60/20/20 by
document (fit / validation / test), and the same test half scores every method.

### Benchmark (8 languages, 336 test documents, ~570 chars each)

| method | accuracy | macro-F1 | ms/doc | note |
|---|---|---|---|---|
| stopword dictionaries | 41.7% | 0.136 | **0.07** | unusable here: the text is mostly names, codes and numbers |
| `langdetect` (1 MB model) | 68.5% | 0.524 | 4.5 | the "ultra light library" is neither accurate nor fast on these stubs |
| char 4-gram centroid | 95.5% | 0.948 | 0.85 | simplest thing that works |
| **naive Bayes, char 4-grams** | **95.8%** | 0.957 | **0.39** | 1 ms to fit, no model file |
| kNN `ball_tree` (sparse) | 96.4% | 0.960 | 2.5 | sklearn silently falls back to brute force: trees cannot index sparse input |
| kNN `kd_tree` (sparse) | 96.4% | 0.960 | 2.6 | same fallback |
| kNN `brute` cosine | 96.4% | 0.960 | 3.2 | reference implementation |
| **kNN `ball_tree` after SVD-64** | **96.7%** | **0.963** | 1.3 | the only way trees actually work: dense projection first |
| kNN `kd_tree` after SVD-64 | 96.7% | 0.963 | 1.3 | equivalent |
| pure-python kNN (k=5) | 96.1% | 0.956 | **66** | same answer, 50x the cost |
| gated fusion of the above | 95.5% | 0.948 | 0.9 | the gate tuned to "never consult the others": fusion does not help |

Two engineering conclusions worth more than the last 1.2 points of accuracy:

* **Trees do not index sparse n-gram vectors.** scikit-learn says so explicitly
  (`cannot use tree with sparse input: using brute force`), so `ball_tree`/`kd_tree` on
  raw hashed features are slower than brute force and identical in accuracy. Reducing to
  64 dense dimensions (truncated SVD, fitted on train) makes the trees real and *faster
  than brute force* (1.3 vs 3.2 ms/doc) with the same best accuracy.
* **Fusion was not worth it.** Tuning the gate on the validation split chose "always trust
  the centroid" — with one method at 96% and the others at 68%, there is nothing to gain.

### Time budget (why this is cheap)

| step | 2k docs | 19k docs (sample+live) | whole corpus (184k) |
|---|---|---|---|
| text extraction, 8 workers | 1.7 s | **16.4 s** | ~2.6 min |
| naive Bayes | 0.8 s | 7.4 s | ~1.2 min |
| centroid | 1.7 s | 16 s | ~2.6 min |
| kNN ball_tree + SVD-64 | 2.6 s | 25 s | ~4 min |
| pure-python kNN | 2.2 min | 21 min | ~3.4 h |

Extraction dominates; the classifier is noise. That is the argument for shipping the
cheap one (naive Bayes) and only paying 2.5x for kNN if a +0.9 pp matters.

### Quality flags

`lang.quality()` is deliberately dumb and threshold-based, because every downstream rule
silently does the wrong thing on a 60-character page:

| flag | rule | meaning |
|---|---|---|
| `no_text` | chars < 40 | text layer missing (scan / image-only): needs OCR, do not parse |
| `sparse` | chars < 200 | cover sheet / notice: parse, but do not expect fields |
| `low_letters` | letters < 35% of chars | extraction garbage, or a pure numeric table |
| `mostly_digits` | digits > 50% | table dump, no labels to pair |

Census over the whole `sample` + `live` corpus (**18,986 documents**):

```
chars   min 271   p10 455   median 567   p90 690   max 921
flags   {}                       zero flagged documents
```

So on this data the guard never fires — which is the point: it is a *guard* for the FINAL
release and for any scanned document, not a filter for the current corpus. If it fires on
more than ~0.1% of a phase, the extraction pipeline is broken, not the document.

The language census also validates the "entity ≠ language" claim:

| entity | language mix | comment |
|---|---|---|
| US01 | en 6,291, it 45, de 34 | |
| ES01 | es 3,493, fr 25, it 24 | |
| FR01 | fr 1,791, pt 21, de 8 | |
| CN01 | **en 550**, fr 10, it 8 | entity country is CN, documents are English |
| MA01 | **fr 221**, pt 3 | Morocco invoices in French |
| CH01 | de 209, en 3, pt 2 | |
| ZA01 | en 2 | the 2025-12 acquisition, no rules yet |

Low-confidence predictions are rare (0-15 per entity) and concentrated exactly there, so
the confidence is usable as a "look at this one" signal even though the fusion itself was
not worth using.

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
