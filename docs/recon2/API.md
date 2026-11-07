# recon2 module APIs (frozen signatures; implement exactly, extend only with extra optional args)

All under `src/recon2/`. Import model types from `recon2.model`. Python 3.12, polars for IO only.

## WP-A data
```python
# money.py
def money(s: str) -> int            # locale-agnostic: last [.,] followed by exactly 2 digits = decimal;
                                    # spaces/NBSP/U+202F/other separators = thousands; '-' sign;
                                    # no 2-digit tail -> integer units*100 if unambiguous, else ValueError
def cents(s: str | None) -> int     # plain dot-decimal CSV amounts ('-1487.60') -> int, '' -> 0 (no float)

# textviews.py
def fold(s: str) -> str             # NFKD -> ASCII upper, keep [A-Z0-9 ./-+], collapse spaces
def build(line: Line) -> None       # fills line.t_join and line.tokens
                                    # t_join: fold(text|ref1|ref2|rinfo|cpty_name|e2e) with n43 38-char wrap glue
                                    # tokens: sorted unique alnum ref-like tokens (>=5 chars, has digit), incl.
                                    #   glued splits (RECHNUNGRE250110905 -> RE250110905) and continuation tails

# fx.py
class FX:
    def __init__(self, masters_dir: Path): ...
    def rate(self, ccy: str, d: int) -> Fraction       # units per EUR, last TARGET day <= d, EUR=1
    def convert(self, cents: int, src: str, dst: str, d: int) -> int   # half-up

# load.py
@dataclass
class Masters:
    customers: dict[str, dict]      # id -> {name, trade_name, country, iban, parent, group, mandate}
    suppliers: dict[str, dict]      # id -> {name, country, iban}
    entity_country: dict[str, str]
    terms: dict[str, tuple[int, int, int]]   # code -> (days, pct_bp(basis points), disc_days)
    accounts: dict[str, dict]       # account_id -> {entity, iban, ccy, purpose}
    fx: FX
@dataclass
class Phase:
    name: str; root: Path; lines: list[Line]; items: list[Item]; masters: Masters
    golden: "pl.DataFrame | None"   # sample/train only
def load_phase(name: str) -> Phase  # 'sample' | 'live' | 'final' | 'train'; lines sorted (day, txn);
                                    # items: phase tables + open_items_at_start (rejoined to TRAIN tables by item_id)

# book.py
class Book:
    def __init__(self, items: list[Item], masters: Masters): ...
    items: list[Item]
    by_id: dict[str, int]
    def by_doc(self, token: str) -> list[int]          # exact variants (alnum, digit core, no leading 0s, PO)
    def by_doc_suffix(self, token: str, scope: set[str] | None = None) -> list[int]   # >=6 chars
    def by_doc_prefix(self, token: str, scope: set[str] | None = None) -> list[int]   # >=7 chars
    def open_of_party(self, party: str, kinds: tuple[str, ...]) -> list[int]          # open items sorted (due, issue, doc, idx)
    def open_by_amount(self, entity: str, ccy: str, cents: int) -> list[int]          # current open balance index
    def apply(self, line: Line, allocs: list[Alloc]) -> None   # mutate open, hist, residue flags, closed_on
    hist: dict[int, list[tuple[int, int, str, int]]]  # item -> [(line_idx, signed applied, exc, day)]
    def settled(self, idx: int) -> bool                # open == 0 (after at least one payment)
```

## WP-B docs
```python
# advices.py
@dataclass
class AdviceRow: doc: str; doc_date: int; gross: int; ded: int; reason: str; paid: int; po: str
@dataclass
class Advice: file: str; entity: str; payer: str; received: int; payment_date: int; ref: str; ccy: str
              total: int; rows: list[AdviceRow]; ok: bool   # ok = Σpaid == stated total and rows parsed
def load_advices(phase_root: Path) -> list[Advice]   # all formats csv/txt/pdf/xlsx incl. Sep-2025 xlsx layout; cached parquet by sha
class AdviceIndex:
    def __init__(self, advices: list[Advice]): ...
    def match(self, line: Line) -> list[Advice]       # payment_date == line.day and total == |cash| (same ccy); ref tie-break

# pdfclauses.py
def load_facts(phase_root: Path) -> dict[tuple[str, str], dict]
    # key (entity, document_number) [AP: (entity, supplier_id + '|' + document_number)] ->
    # {'instalments': {'n','amounts'}, 'retention': {'pct','amount'}, 'advance': {'amount'},
    #  'replaces': doc, 'service_month': 'YYYY-MM', 'rappel': {'dir'}, 'scn': 'offset'|'refund',
    #  'cancel_cn': doc, 'assignment': factor, 'iban_change': {'new','old','effective','mandate','dir'}}
    # cached parquet/json by sha; LIVE gate counts in DESIGN/brief_docs
```

## WP-C routing
```python
# treasury.py
def classify(line: Line) -> str | None     # CASH_POOL | INTERNAL_TRANSFER | FX_DEAL | INTERCOMPANY | None
# cats.py
def learn(train_root: Path, until: str, out: Path) -> None   # artefact json: key -> category counts (back-off levels)
class Router:
    def __init__(self, artefact: Path): ...
    def category(self, line: Line) -> str   # never '' ; credit default UNIDENTIFIED, debit default BANK_FEES
    def is_category_line(self, line: Line) -> float   # prior P(category-only), informational
# party.py
class Parties:
    def __init__(self, masters: Masters, aliases: Path | None, notices: dict | None): ...
    def resolve(self, line: Line, side: str) -> list[tuple[str, str]]   # [(party_id, source)] sorted by precedence; side 'AR'|'AP'
    def third_party_doc_hint(self, line: Line) -> list[str]              # doc fragments from EXPTE/ORD. FRA ... VTO
```

## WP-D accounting
```python
# decide.py
@dataclass
class Ctx: masters: Masters; wht_rate: dict[str, int]   # customer -> rate in basis points (A3)
def decide(line: Line, hyp: Hyp, book: Book, ctx: Ctx, applied: dict[int, int]) -> list[Alloc]
    # applied: item idx -> cash applied in ITEM ccy (already split by the resolver);
    # returns final rows (amount + exception + rule) following DESIGN §4, line-level E7/E8
def split_cash(line: Line, hyp: Hyp, book: Book, ctx: Ctx) -> dict[int, int] | None
    # allocates |cash| over hyp.items (+cn) in item ccy; None if arithmetic does not fit (fit rules §1)
# dev/oracle.py : python -m recon2.dev.oracle -> per-class confusion of decide() on verifier sample
```

## WP-E ops
```python
# ops.py
class Ops:
    def __init__(self, phase: Phase, extra_roots: list[Path]): ...   # dd batches, cards, cheques, payment runs (global index)
    def resolve(self, line: Line, book: Book) -> Hyp | None           # T0; enforces amount gate & direction
# returns.py
class Returns:
    def __init__(self, phase: Phase, ops: Ops): ...
    def resolve(self, line: Line, book: Book) -> Hyp | None           # T1 RETURNED hyp (is_return=True)
    def represent(self, line: Line, book: Book, parties: list[str]) -> Hyp | None   # re-payment of reopened item
```
