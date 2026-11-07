"""PDF clause facts: closed EN+ES templates on wrap-joined pypdf text.

Facts are evidence only (CLAUDE.md §15c).  Keys come from pdfs/index.csv (never from the PDF):
AR/proforma/notice -> (entity, document_number); AP -> (entity, supplier_id + '|' + document_number).
Clauses exist only in English or Spanish (DOCS brief A).
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
from concurrent.futures import ProcessPoolExecutor
from datetime import date
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "cache"
_VERSION = "pdf-v1"

_SEP = re.compile(r"[\s  '’]")


def _money(s: str) -> int:
    """Locale-agnostic: last [.,] followed by exactly 2 digits = decimal; others thousands."""
    s = _SEP.sub("", s.strip()).rstrip(".")
    neg = s.startswith("-")
    s = s.strip("-+")
    m = re.fullmatch(r"([\d.,]*?)[.,](\d{2})", s)
    if m:
        v = int(re.sub(r"[.,]", "", m.group(1)) or "0") * 100 + int(m.group(2))
    elif re.fullmatch(r"\d+", s):
        v = int(s) * 100
    else:
        raise ValueError(f"bad amount {s!r}")
    return -v if neg else v


def _dmy(s: str) -> str:
    d, m, y = s.split("/")
    return date(int(y), int(m), int(d)).isoformat()


DOC = r"([A-Z0-9][A-Z0-9/-]*\d)"
AMT = r"(-?\d[\d.,\s  ]*\d)"
IBAN = r"([A-Z]{2}\d{2}[A-Z0-9]{10,30})"
DATE = r"(\d{2}/\d{2}/\d{4})"
_MONTHS = {m: i + 1 for i, m in enumerate(
    "january february march april may june july august september october november december".split())}
_MONTHS.update({m: i + 1 for i, m in enumerate(
    "enero febrero marzo abril mayo junio julio agosto septiembre octubre noviembre diciembre".split())})
_MONTHS["setiembre"] = 9

R_INST = re.compile(r"(\d+) (?:instalments at|vencimientos a) ([\d/]+) (?:days|días) \(([^)]*)\)")
R_RET = re.compile(r"(?:Retention|Retención en garantía del) (\d+(?:[.,]\d+)?) ?% \(" + AMT + r" ?[A-Z]{3}\)"
                   r".*?(?:until final acceptance|a la recepción definitiva) \(" + DATE + r"\)"
                   r"(?:.*?(?:Amount payable at due date|Importe a pagar al vencimiento): " + AMT + r")?")
R_ADV = re.compile(r"(?:Less advance received against proforma|A deducir anticipo recibido según proforma) "
                   r"(\S+?):? (?:de )?" + AMT + r" ?[A-Z]{3}\.? (?:Net amount due|Importe neto a pagar) " + AMT)
R_REPL = re.compile(r"(?:cancels and replaces invoice|anula y sustituye a la factura) " + DOC)
R_MONTH = re.compile(r"(?:service fee for the month of|servicio técnico correspondiente a) ([A-Za-zñ]+) (?:de )?(\d{4})")
R_RAPPEL = re.compile(r"(?:Volume rebate|Rappel) .{0,120}?\):? ?([^.]*)")
R_SCN_OFF = re.compile(r"deducted from your next payment|se compensará en sus próximos pagos")
R_SCN_REF = re.compile(r"refunded by bank transfer|reembolsado mediante transferencia")
R_CANCEL = re.compile(r"(?:CANCELLING OF INVOICE N\S*|anulación total de la factura) " + DOC)
R_ASSIGN = re.compile(r"(?:has been assigned to|ha sido cedido a) (.+?)\.(?:\s|$)")
R_IBANCHG = re.compile(r"(?:from|a partir del) " + DATE + r" (?:our bank details change|nuestros datos bancarios cambian"
                       r"|our payments|los recibos|nuestros pagos|our direct debits|the direct debits)")
R_MANDATE = re.compile(r"(?:Referencia de mandato|Mandate reference):? (\S+?)\.?(?:\s|$)")
R_TOTAL = re.compile(r"\b(?:TOTAL TTC|TOTAL|GESAMT|TOTALE)\s*:?\s*" + AMT)


def extract(text: str, doc_type: str = "") -> dict:
    """Clause facts from one PDF's text (pure; tested without files)."""
    b = re.sub(r"\s*\n\s*", " ", text)
    f: dict = {}
    m = R_TOTAL.search(b)
    if m:
        try:
            f["total"] = _money(m.group(1))
        except ValueError:
            pass
    m = R_INST.search(b)
    if m:
        parts = [re.sub(r"\s*[A-Z]{3}$", "", p.strip()) for p in m.group(3).split(" / ")]
        try:
            f["instalments"] = {"n": int(m.group(1)), "days": [int(x) for x in m.group(2).split("/")],
                                "amounts": [_money(p) for p in parts]}
        except ValueError:
            f["instalments"] = {"n": int(m.group(1)), "days": [], "amounts": []}
    m = R_RET.search(b)
    if m:
        f["retention"] = {"pct": m.group(1).replace(",", "."), "amount": _money(m.group(2)),
                          "release": _dmy(m.group(3)), "payable": _money(m.group(4)) if m.group(4) else 0}
    m = R_ADV.search(b)
    if m:
        f["advance"] = {"proforma": m.group(1), "amount": _money(m.group(2)), "net": _money(m.group(3))}
    m = R_REPL.search(b)
    if m:
        f["replaces"] = m.group(1)
    m = R_MONTH.search(b)
    if m and m.group(1).lower() in _MONTHS:
        f["service_month"] = f"{m.group(2)}-{_MONTHS[m.group(1).lower()]:02d}"
    m = R_RAPPEL.search(b)
    if m:
        tail = m.group(1)
        d = ("offset" if re.search(r"offset against|se compensará", tail) else
             "payout" if re.search(r"paid to your bank|abonará por transferencia", tail) else "")
        f["rappel"] = {"dir": d}
    if R_SCN_OFF.search(b):
        f["scn"] = "offset"
    elif R_SCN_REF.search(b):
        f["scn"] = "refund"
    m = R_CANCEL.search(b)
    if m:
        f["cancel_cn"] = m.group(1)
    m = R_ASSIGN.search(b)
    if m:
        f["assignment"] = m.group(1).strip()
    m = R_IBANCHG.search(b)
    if m:
        ibans = re.findall(IBAN, b[m.end():])
        mand = R_MANDATE.search(b)
        tail = b[m.start():m.start() + 200]
        f["iban_change"] = {
            "new": ibans[0] if ibans else "", "old": ibans[1] if len(ibans) > 1 else "",
            "effective": _dmy(m.group(1)), "mandate": mand.group(1) if mand else "",
            "dir": ("payee" if re.search(r"bank details change|datos bancarios", tail) else
                    "dd" if re.search(r"recibos domiciliados|direct debit", tail, re.I) else "payer"),
        }
    return f


def _text(path: str) -> str:
    import pypdf
    return "\n".join((pg.extract_text() or "") for pg in pypdf.PdfReader(path).pages)


def _work(args: tuple[str, str]) -> dict:
    path, doc_type = args
    try:
        return extract(_text(path), doc_type)
    except Exception as e:  # unreadable pdf: no facts
        return {"error": type(e).__name__}


_AP = ("supplier_invoice", "supplier_credit_note")


def _key(r: dict) -> tuple[str, str]:
    if r["document_type"] in _AP:
        sup = r.get("party_id") or Path(r["file"]).name.split("_")[1]
        return (r["entity"], f"{sup}|{r['document_number']}")
    if not r["document_number"]:  # bank_account_notice: no number, several per entity
        return (r["entity"], f"NOTICE|{r.get('party_id', '')}|{r['file']}")
    return (r["entity"], r["document_number"])


def notices(facts: dict[tuple[str, str], dict]) -> dict[str, list[dict]]:
    """party_id -> iban_change facts (bank_account_notice + supplier invoice bank change), by effective date."""
    out: dict[str, list[dict]] = {}
    for (_, _), f in sorted(facts.items()):
        if "iban_change" in f and f.get("party"):
            out.setdefault(f["party"], []).append(dict(f["iban_change"], type=f["type"]))
    for v in out.values():
        v.sort(key=lambda x: (x["effective"], x["new"]))
    return out


def _sha(root: Path) -> str:
    h = hashlib.sha256(_VERSION.encode())
    h.update(Path(__file__).read_bytes())
    h.update((root / "index.csv").read_bytes())
    for f in sorted(root.rglob("*.pdf")):
        h.update(f"{f.relative_to(root)}|{f.stat().st_size}".encode())
    return h.hexdigest()[:16]


def load_facts(phase_root: Path, cache: bool = True, workers: int = 8) -> dict[tuple[str, str], dict]:
    root = Path(phase_root) / "pdfs"
    if not (root / "index.csv").exists():
        return {}
    path = CACHE_DIR / f"pdffacts_{Path(phase_root).name}_{_sha(root)}.json"
    if cache and path.exists():
        return {(e, d): f for e, d, f in json.loads(path.read_text())}
    with open(root / "index.csv", newline="", encoding="utf-8") as fh:
        idx = sorted(csv.DictReader(fh), key=lambda r: r["file"])
    jobs = [(str(root / r["file"]), r["document_type"]) for r in idx]
    with ProcessPoolExecutor(workers) as ex:
        res = list(ex.map(_work, jobs, chunksize=64))
    out: dict[tuple[str, str], dict] = {}
    for r, f in zip(idx, res):
        f = dict(f, type=r["document_type"], party=r.get("party_id", ""), file=r["file"])
        out[_key(r)] = f
    if cache:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(sorted([e, d, f] for (e, d), f in out.items()), ensure_ascii=False))
        tmp.replace(path)
    return out


if __name__ == "__main__":  # gate: python -m recon2.pdfclauses live
    import sys
    import time
    from collections import Counter

    from recon2.model import DATA

    for ph in sys.argv[1:] or ["sample"]:
        t = time.time()
        facts = load_facts(DATA / ph, cache=False)
        dt = time.time() - t
        c: Counter = Counter()
        for f in facts.values():
            for k in ("instalments", "retention", "advance", "replaces", "service_month", "cancel_cn",
                      "assignment", "error"):
                c[k] += k in f
            if "rappel" in f:
                c["rappel_" + (f["rappel"]["dir"] or "?")] += 1
            if "scn" in f:
                c["scn_" + f["scn"]] += 1
            if "iban_change" in f:
                c["iban_" + f["type"]] += 1
            ins = f.get("instalments")
            if ins:
                c["inst_sum_ok"] += bool(ins["amounts"]) and len(ins["amounts"]) == ins["n"] and \
                    abs(sum(ins["amounts"]) - f.get("total", -10)) <= 1
            r = f.get("retention")
            if r:
                c["ret_sum_ok"] += abs(r["amount"] + r["payable"] - f.get("total", -10)) <= 1
            a = f.get("advance")
            if a:
                c["adv_sum_ok"] += abs(a["amount"] + a["net"] - f.get("total", -10)) <= 1
        print(f"{ph}: {len(facts)} pdfs {dt:.1f}s", dict(sorted(c.items())))
