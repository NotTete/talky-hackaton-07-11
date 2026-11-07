"""Remittance advices (csv / txt / pdf / xlsx, incl. the Sep-2025 Voucher|Invoice xlsx layout).

An advice is evidence: payer, payment_ref, payment_date (from the advice content, never
the index received_date), stated total and per-document rows.  `ok` means every row parsed
and Σpaid == stated total.  Linkage to bank lines is on payment_date == booking day and
|cash| == total (same ccy), payment_ref token as tie-break (verifier H7: 100% same day).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

import polars as pl

from recon2.model import Line, day

CACHE_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "cache"
_VERSION = "adv-v1"


@dataclass(slots=True)
class AdviceRow:
    doc: str
    doc_date: int
    gross: int
    ded: int
    reason: str
    paid: int
    po: str
    alias: str = ""          # Sep-2025 xlsx: voucher number (invoice column wins as doc)


@dataclass(slots=True)
class Advice:
    file: str
    entity: str
    payer: str
    received: int
    payment_date: int
    ref: str
    ccy: str
    total: int
    rows: list[AdviceRow] = field(default_factory=list)
    ok: bool = False
    fmt: str = ""


# ----------------------------------------------------------------------------- money

_SEP = re.compile(r"[\s  '’]")


def _money(s: str) -> int:
    """Locale-agnostic: last [.,] followed by exactly 2 digits = decimal; others thousands."""
    s = _SEP.sub("", s.strip())
    neg = s.startswith("-") or s.endswith("-")
    s = s.strip("-+")
    m = re.fullmatch(r"([\d.,]*?)[.,](\d{2})", s)
    if m:
        whole, dec = re.sub(r"[.,]", "", m.group(1)) or "0", m.group(2)
    else:
        if not re.fullmatch(r"\d{1,3}([.,]\d{3})*|\d+", s):
            raise ValueError(f"bad amount {s!r}")
        whole, dec = re.sub(r"[.,]", "", s), "00"
    v = int(whole) * 100 + int(dec)
    return -v if neg else v


def _dec(s: str) -> int:
    """Dot-decimal machine number ('5079.2', '-18.38', '0') -> cents, half-up, no float."""
    s = s.strip()
    if not s:
        return 0
    return int((Decimal(s) * 100).quantize(Decimal(1), rounding="ROUND_HALF_UP"))


def _dmy(s: str, us: bool = False) -> int:
    s = s.strip()
    if re.fullmatch(r"\d{4}-\d\d-\d\d", s):
        return day(s)
    m = re.fullmatch(r"(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})", s)
    if not m:
        return 0
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if y < 100:
        y += 2000
    d, mo = (b, a) if us else (a, b)
    try:
        return date(y, mo, d).toordinal()
    except ValueError:
        return 0


# ----------------------------------------------------------------------------- headers

_HDR = {
    "doc": ("document", "documento", "invoice", "factura", "doc"),
    "voucher": ("voucher",),
    "date": ("doc_date", "date", "fecha", "inv. date", "inv date", "datum"),
    "gross": ("gross", "bruto", "amount", "importe"),
    "ded": ("discount_or_deduction", "deduction", "dto./ded.", "descuento"),
    "reason": ("reason", "motivo"),
    "paid": ("paid", "pagado", "net paid", "net"),
    "po": ("your_order", "po", "pedido", "su pedido"),
}


def _colmap(hdr: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for i, h in enumerate(hdr):
        h = h.strip().lower()
        for k, syn in _HDR.items():
            if h in syn and k not in out:
                out[k] = i
                break
    return out


def _row_from_cells(cells: list[str], cm: dict[str, int], num) -> AdviceRow:
    def g(k: str) -> str:
        i = cm.get(k)
        return cells[i].strip() if i is not None and i < len(cells) and cells[i] is not None else ""

    gross = num(g("gross")) if g("gross") else 0
    ded = num(g("ded")) if g("ded") else 0
    paid = num(g("paid")) if g("paid") else gross + ded
    doc, alias = g("doc"), g("voucher")
    if not doc:
        doc, alias = alias, ""
    return AdviceRow(doc, _dmy(g("date")), gross, ded, g("reason"), paid, g("po"), alias)


# ----------------------------------------------------------------------------- parsers

def _csv(p: Path, a: Advice) -> None:
    text = p.read_text(encoding="utf-8", errors="replace")
    first = text.split("\n", 1)[0]
    sep = ";" if first.count(";") > first.count(",") else ","
    rows = [r for r in csv.reader(io.StringIO(text), delimiter=sep) if any(c.strip() for c in r)]
    meta = rows[0]
    md = {meta[i].strip().lower(): meta[i + 1].strip() for i in range(0, len(meta) - 1, 2)}
    a.ref = md.get("payment_ref", "")
    a.payment_date = _dmy(md.get("payment_date", ""))
    a.ccy = md.get("currency", "")
    cm = _colmap(rows[1])
    total = None
    for r in rows[2:]:
        if r[0].strip().upper() == "TOTAL":
            vals = [c for c in r[1:] if c.strip()]
            total = _money(vals[-1]) if vals else None
            continue
        a.rows.append(_row_from_cells(r, cm, _money))
    a.total = total if total is not None else sum(r.paid for r in a.rows)
    a.ok = total is not None


_TXT_ROW = re.compile(r"^\s{2}(\S+)\s+(\d\d/\d\d/\d\d(?:\d\d)?)\s+(-?\d[\d.,]*)(?:\s+(-\d[\d.,]*)(?:\s+(.*))?)?\s*$")
_TXT_EN = re.compile(r"payment of\s+([-\d.,\s  ]+?)\s+([A-Z]{3})\s+on\s+(\d{1,2}/\d{1,2}/\d{4})")
_TXT_ES = re.compile(r"importe de\s+([-\d.,\s  ]+?)\s+([A-Z]{3})\s+con fecha\s+(\d{1,2}/\d{1,2}/\d{4})")


def _txt(p: Path, a: Advice) -> None:
    text = p.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^Subject:.*?(?:advice|pago|Zahlungsavis|paiement)\s+(\S+)\s*$", text, re.M | re.I)
    if not m:
        m = re.search(r"^Subject:.*\s(\S+)\s*$", text, re.M)
    a.ref = m.group(1) if m else ""
    body = re.sub(r"\s*\n(?!\s{2}\S)\s*", " ", text)  # join wrapped sentences, keep table rows
    stated = None
    m = _TXT_EN.search(body)
    if m:
        stated, a.ccy, a.payment_date = _money(m.group(1)), m.group(2), _dmy(m.group(3), us=True)
    else:
        m = _TXT_ES.search(body)
        if m:
            stated, a.ccy, a.payment_date = _money(m.group(1)), m.group(2), _dmy(m.group(3))
    bad = 0
    for ln in text.split("\n"):
        if not ln.startswith("  ") or not ln.strip():
            continue
        r = _TXT_ROW.match(ln.rstrip())
        if not r:
            bad += 1
            continue
        gross = _money(r.group(3))
        ded = _money(r.group(4)) if r.group(4) else 0
        a.rows.append(AdviceRow(r.group(1), _dmy(r.group(2)), gross, ded, (r.group(5) or "").strip(),
                                gross + ded, ""))
    a.total = stated if stated is not None else sum(r.paid for r in a.rows)
    a.ok = stated is not None and not bad


_PDF_FOOT = ("Documento sintético", "Synthetic test document")
_PDF_PAID = ("Paid", "Pagado", "Net paid")
_DATE_CELL = re.compile(r"\d{2}/\d{2}/\d{4}|\d{4}-\d\d-\d\d")
_NUM_CELL = re.compile(r"-?[\d.,\s  ]*[.,]\d\d")   # money cell (docs may be pure digits)


def _pdf(p: Path, a: Advice) -> None:
    import pypdf

    cells = [c.strip() for pg in pypdf.PdfReader(str(p)).pages
             for c in (pg.extract_text() or "").split("\n") if c.strip()]
    cells = [c for c in cells if not any(f in c for f in _PDF_FOOT)]
    lab = {c: i for i, c in reversed(list(enumerate(cells)))}

    def after(*names: str) -> str:
        for n in names:
            if n in lab and lab[n] + 1 < len(cells):
                return cells[lab[n] + 1]
        return ""

    a.ref = after("Payment ref.", "Ref.", "Referencia")
    a.payment_date = _dmy(after("Date", "Fecha"))
    am = after("Amount", "Importe")
    stated = None
    m = re.fullmatch(r"(-?[\d.,\s  ]+?)\s*([A-Z]{3})", am)
    if m:
        stated, a.ccy = _money(m.group(1)), m.group(2)
    hdr = max((i for i, c in enumerate(cells) if c in _PDF_PAID), default=-1)
    body = cells[hdr + 1:] if hdr >= 0 else []
    starts = [i for i in range(len(body) - 1) if _DATE_CELL.fullmatch(body[i + 1]) and not _NUM_CELL.fullmatch(body[i])]
    bad = 0
    for k, s in enumerate(starts):
        e = starts[k + 1] if k + 1 < len(starts) else len(body)
        vals = body[s + 2:e]
        try:
            gross, paid = _money(vals[0]), _money(vals[-1])
            ded, reason = 0, ""
            if len(vals) >= 3:
                mm = re.match(r"(-?[\d.,\s  ]*\d)\s*(.*)", " ".join(vals[1:-1]))
                ded, reason = (_money(mm.group(1)), mm.group(2).strip()) if mm else (0, " ".join(vals[1:-1]))
        except (ValueError, IndexError):
            bad += 1
            continue
        a.rows.append(AdviceRow(body[s], _dmy(body[s + 1]), gross, ded, reason, paid, ""))
    a.total = stated if stated is not None else sum(r.paid for r in a.rows)
    a.ok = stated is not None and not bad and hdr >= 0


def _col(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def _xlsx(p: Path, a: Advice) -> None:
    z = zipfile.ZipFile(p)
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        ss = z.read("xl/sharedStrings.xml").decode()
        shared = ["".join(re.findall(r"<t[^>]*>([^<]*)</t>", si)) for si in re.findall(r"<si>(.*?)</si>", ss, re.S)]
    x = z.read("xl/worksheets/sheet1.xml").decode()
    grid: list[list[str]] = []
    for row in re.findall(r"<row[^>]*>(.*?)</row>", x, re.S):
        out: list[str] = []
        for m in re.finditer(r"<c r=\"([A-Z]+)\d+\"([^>]*?)(?:/>|>(.*?)</c>)", row, re.S):
            ci, attrs, inner = _col(m.group(1)), m.group(2), m.group(3) or ""
            t = re.search(r"<t[^>]*>([^<]*)</t>", inner)
            v = re.search(r"<v>([^<]*)</v>", inner)
            val = t.group(1) if t else (v.group(1) if v else "")
            if 't="s"' in attrs and v:
                val = shared[int(v.group(1))]
            while len(out) <= ci:
                out.append("")
            out[ci] = _unxml(val)
        grid.append(out)
    hdr_i = None
    for i, r in enumerate(grid):
        low = [c.strip().lower() for c in r]
        if r and low[0] in ("payment advice", "aviso de pago") and len(r) > 1:
            a.ref = r[1].strip()
            a.payment_date = _dmy(r[2]) if len(r) > 2 else 0
        if hdr_i is None and any(c in ("document", "documento", "voucher", "invoice") for c in low) and \
                any(c in ("paid", "pagado", "net paid") for c in low):
            hdr_i = i
    if hdr_i is None:
        return
    cm = _colmap(grid[hdr_i])
    total = None
    for r in grid[hdr_i + 1:]:
        if not any(c.strip() for c in r):
            continue
        if r[0].strip().upper() == "TOTAL":
            vals = [c for c in r[1:] if c.strip()]
            total = _dec(vals[-1]) if vals else None
            continue
        a.rows.append(_row_from_cells(r, cm, _dec))
    a.total = total if total is not None else sum(r.paid for r in a.rows)
    a.ok = total is not None


def _unxml(s: str) -> str:
    return s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'")


_PARSERS = {"csv": _csv, "txt": _txt, "pdf": _pdf, "xlsx": _xlsx}


# ----------------------------------------------------------------------------- load + cache

def _parse_all(root: Path) -> list[Advice]:
    ix = pl.read_csv(root / "index.csv", infer_schema_length=0)
    out: list[Advice] = []
    for r in ix.iter_rows(named=True):
        fmt = (r["format"] or Path(r["file"]).suffix[1:]).lower()
        a = Advice(r["file"], r["entity"], r["payer_name"] or "", day(r["received_date"]), 0, "", "", 0, fmt=fmt)
        try:
            _PARSERS[fmt](root / r["file"], a)
        except Exception:  # a broken file is an advice we cannot use, not a crash
            a.ok = False
        if a.ok:
            a.ok = bool(a.rows) and a.payment_date > 0 and sum(x.paid for x in a.rows) == a.total
        out.append(a)
    out.sort(key=lambda a: a.file)
    return out


def _sha(root: Path) -> str:
    h = hashlib.sha256(_VERSION.encode())
    h.update((Path(__file__).read_bytes()))
    h.update((root / "index.csv").read_bytes())
    for f in sorted(root.rglob("*")):
        if f.is_file():
            h.update(f"{f.relative_to(root)}|{f.stat().st_size}".encode())
    return h.hexdigest()[:16]


_A_COLS = ("file", "entity", "payer", "received", "payment_date", "ref", "ccy", "total", "ok", "fmt")
_R_COLS = ("doc", "doc_date", "gross", "ded", "reason", "paid", "po", "alias")


def _to_df(advs: list[Advice]) -> pl.DataFrame:
    recs = []
    for a in advs:
        base = {k: getattr(a, k) for k in _A_COLS}
        base["rows"] = [{k: getattr(r, k) for k in _R_COLS} for r in a.rows]
        recs.append(base)
    return pl.DataFrame(recs, schema={
        "file": pl.Utf8, "entity": pl.Utf8, "payer": pl.Utf8, "received": pl.Int64, "payment_date": pl.Int64,
        "ref": pl.Utf8, "ccy": pl.Utf8, "total": pl.Int64, "ok": pl.Boolean, "fmt": pl.Utf8,
        "rows": pl.List(pl.Struct({"doc": pl.Utf8, "doc_date": pl.Int64, "gross": pl.Int64, "ded": pl.Int64,
                                   "reason": pl.Utf8, "paid": pl.Int64, "po": pl.Utf8, "alias": pl.Utf8})),
    })


def _from_df(df: pl.DataFrame) -> list[Advice]:
    out = []
    for d in df.iter_rows(named=True):
        rows = [AdviceRow(**r) for r in d.pop("rows")]
        out.append(Advice(rows=rows, **d))
    return out


def load_advices(phase_root: Path, cache: bool = True) -> list[Advice]:
    root = Path(phase_root) / "remittance_advices"
    if not (root / "index.csv").exists():
        return []
    path = CACHE_DIR / f"advices_{Path(phase_root).name}_{_sha(root)}.parquet"
    if cache and path.exists():
        return _from_df(pl.read_parquet(path))
    advs = _parse_all(root)
    if cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".{id(advs)}.tmp")
        _to_df(advs).write_parquet(tmp)
        tmp.replace(path)
    return advs


# ----------------------------------------------------------------------------- index

def _norm_ref(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", s.upper())


class AdviceIndex:
    def __init__(self, advices: list[Advice]):
        self.by_key: dict[tuple[int, str, int], list[Advice]] = {}
        for a in advices:
            if a.payment_date and a.total:
                self.by_key.setdefault((a.payment_date, a.ccy, abs(a.total)), []).append(a)
        for v in self.by_key.values():
            v.sort(key=lambda a: (not a.ok, a.file))

    def match(self, line: Line) -> list[Advice]:
        # xlsx advices carry no currency (''); a cross-ccy payment matches on the instructed amount
        keys = [(line.day, line.ccy, abs(line.cash)), (line.day, "", abs(line.cash))]
        if line.orig and line.orig_ccy and line.orig_ccy != line.ccy:
            keys += [(line.day, line.orig_ccy, abs(line.orig)), (line.day, "", abs(line.orig))]
        c: list[Advice] = []
        for k in keys:
            c += [a for a in self.by_key.get(k, []) if a not in c]
        if len(c) <= 1:
            return list(c)
        hay = _norm_ref(" ".join((line.t_join, line.text, line.ref1, line.ref2, line.rinfo, line.e2e)))
        hit = [a for a in c if a.ref and len(_norm_ref(a.ref)) >= 5 and _norm_ref(a.ref) in hay]
        return hit + [a for a in c if a not in hit]


if __name__ == "__main__":  # gate report: python -m recon2.advices sample live
    import sys
    import time
    from collections import Counter

    from recon2.model import DATA

    for ph in sys.argv[1:] or ["sample"]:
        t = time.time()
        advs = load_advices(DATA / ph, cache=False)
        dt = time.time() - t
        st: Counter = Counter()
        for a in advs:
            st[(a.fmt, "n")] += 1
            st[(a.fmt, "ok")] += a.ok
            st[(a.fmt, "ref")] += bool(a.ref)
            st[(a.fmt, "rows")] += len(a.rows)
            st[(a.fmt, "date")] += a.payment_date > 0
        n = len(advs)
        print(f"{ph}: {n} advices, ok {sum(a.ok for a in advs)/max(n,1):.4f}, {dt:.1f}s")
        for f in sorted({k[0] for k in st}):
            print(f"  {f}: n={st[f,'n']} ok={st[f,'ok']} ref={st[f,'ref']} date={st[f,'date']} rows={st[f,'rows']}")
