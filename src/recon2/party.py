"""Party resolution (DESIGN §3, T4/T5): which customers/suppliers could have sent/received this line.

Returns a party SET (names are not keys: 7k names are shared by 23k customers), ordered by source
precedence.  A third-party marker (collection agency EXPTE, confirming ORD. ... FRA ... VTO, POR CTA DE)
REPLACES the counterparty: the agency/factor is never the party.  Otherwise the union of
IBAN > N43 8-digit ordering-id alias > camt counterparty name > payer markers > learned name alias,
and only if all of those are empty, a rarest-token name search over the whole text.
AR parties are expanded with children(payer) via parent_customer_id (not group_id).
Everything O(masters) is built once in __init__; per line is a handful of dict lookups.
"""

from __future__ import annotations

import json
import re
import unicodedata
from bisect import bisect_left
from collections import defaultdict
from pathlib import Path

from recon2.model import Line

MAX_SET = 6        # token-intersection blocking
PREFIX_CAP = 30    # exact/prefix full-name match (same name shared by many customers is legitimate)
AP_POOL = {"ES00": ("ES00", "ES01", "ES02", "PT01")}   # payment factory pays for these entities

AGENCIES = ("ARVENTO RECOVERIES", "RECOBROS AGORA", "DELTACOBRO", "GESTION DE IMPAGADOS")
FACTORS = ("CONFIRMING PENALARA", "CREDIFACIL COMERCIAL", "RIBAGORZA CONFIRMING", "NORTE PAGOS CONFIRMIN")
MARKERS = ("TRANSFERENCIA DE ", "TRANSFERENCIAS ", "ORDENANTE ", "ORD. ", "/ORDP/", "ABWA+", "VIREMENT DE ",
           "VIR SEPA RECU /DE ", "DE LA PART DE ", "VON ", "SEPA-UEBERWEISUNG AN ", "BONIFICO DA ",
           "TRANSFERENCIA A ", "VIR SEPA EMIS /A ")
_MARK_END = re.compile(r" \| |/| [A-Z]{4}\+| CONCEPTO| FRA\b| REF\b| MOTIF")
_STOP = frozenset("""S L U A SA SL SLU SAS SARL EURL SPA SRL SRLS LTD LLC INC CORP CO KG GMBH AG BV NV PTY EIRELI LDA
 ME COOP SCOOP SOC DE DEL LA EL LOS LAS Y E ET DA DO DOS DAS DI DES DU LE THE AND OF UND VON Y CIA HIJOS HERMANOS
 GROUP GRUPO PLC LIMITED SC SCA SNC SASU OHG MBH UG CV SAPI RL BHD PTE ZOO SP OY AB AS""".split())
_ID8 = re.compile(r"(?:^| )(\d{8})-([^|]+)")
_EXPTE = re.compile(r"EXPTE (\d+) (.*?)(?: (\d{5,}))?(?= \||$| POR CTA| ?/| [A-Z]{4}\+)")
_PORCTA = re.compile(r"POR CTA DE ([^|]+)")
_CONF = re.compile(r"ORD\. (.+?) FRA (.+?) VTO (\d{6})")
_CONF_OPEN = re.compile(r"ORD\. (.+?) FRA (\S.*)$")
_IBAN = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{10,30})\b")


def fold(s: str | None) -> str:
    """Upper ASCII without accents, '|' kept (N43 fragment separator), spaces collapsed."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode().upper()
    return " ".join(s.split())


def glue(s: str) -> str:
    """N43 wraps text at 38 chars: a ' | ' after a 38-char fragment is a wrap, glue without a space."""
    if " | " not in s:
        return s
    parts = s.split(" | ")
    out = parts[0]
    prev = parts[0]
    for p in parts[1:]:
        out += ("" if len(prev) == 38 else " | ") + p
        prev = p
    return out


def name_tokens(s: str) -> list[str]:
    return [t for t in re.findall(r"[A-Z0-9]+", s) if len(t) >= 2 and not t.isdigit() and t not in _STOP]


def _norm_iban(s: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (s or "").upper())


def _is_creditor_id(iban: str) -> bool:
    return "ZZZ" in iban[4:10]   # SEPA creditor identifiers (DE98ZZZ..., ES..00ZZZ...) are not accounts


def _entity_of(pid: str, rec: dict) -> str:
    return rec.get("entity") or pid[1:5]


class _NameIndex:
    """(entity, token) -> sorted party ids; rarest-token-first intersection."""

    def __init__(self, parties: dict[str, dict]):
        post: dict[tuple[str, str], set[str]] = defaultdict(set)
        for pid, rec in parties.items():
            ent = _entity_of(pid, rec)
            for field in ("name", "trade_name"):
                for t in name_tokens(fold(rec.get(field))):
                    post[(ent, t)].add(pid)
        self.post = {k: frozenset(v) for k, v in post.items()}
        full: dict[str, set[str]] = defaultdict(set)     # "ENT|NAME KEY" -> parties (names are shared)
        for pid, rec in parties.items():
            ent = _entity_of(pid, rec)
            for field in ("name", "trade_name"):
                k = " ".join(name_tokens(fold(rec.get(field))))
                if k:
                    full[f"{ent}|{k}"].add(pid)
        self.full = {k: sorted(v) for k, v in full.items()}
        self.keys = sorted(self.full)

    def prefix(self, text: str, ents: tuple[str, ...]) -> list[str]:
        """Banks truncate the payer name (16/35/38 chars): exact name key, else names starting with it."""
        q = " ".join(name_tokens(text))
        if len(q) < 6:
            return []
        out: set[str] = set()
        for e in ents:
            k = f"{e}|{q}"
            if k in self.full:
                out.update(self.full[k])
                continue
            i = bisect_left(self.keys, k)
            n = 0
            while i < len(self.keys) and self.keys[i].startswith(k) and n <= PREFIX_CAP:
                out.update(self.full[self.keys[i]])
                i += 1
                n += 1
        return sorted(out) if len(out) <= PREFIX_CAP else []

    def lookup(self, text: str, ents: tuple[str, ...], allow_single: bool = False) -> list[str]:
        toks = list(dict.fromkeys(name_tokens(text)))
        if not toks:
            return []
        plist = []
        for t in toks:
            s: frozenset[str] | set[str] = frozenset()
            for e in ents:
                p = self.post.get((e, t))
                if p:
                    s = s | p
            if s:
                plist.append((len(s), t, s))
        if not plist:
            return []
        plist.sort(key=lambda x: (x[0], x[1]))
        cur = plist[0][2]
        matched = 1
        for _, _, s in plist[1:]:
            inter = cur & s
            if inter:
                cur = inter
                matched += 1
        if len(cur) > MAX_SET:
            return []
        if matched < 2 and not (allow_single or len(toks) == 1):
            return []
        return sorted(cur)


class Parties:
    def __init__(self, masters, aliases: Path | None = None, notices: dict | None = None):
        """notices: optional {iban: [party_id, ...]} from bank-account-change notices (old and new IBANs)."""
        self.cust: dict[str, dict] = masters.customers
        self.supp: dict[str, dict] = masters.suppliers
        self.iban: dict[str, list[str]] = defaultdict(list)
        for book in (self.cust, self.supp):
            for pid, rec in sorted(book.items()):
                ib = _norm_iban(rec.get("iban"))
                if ib and not _is_creditor_id(ib):
                    self.iban[ib].append(pid)
        for ib, pids in sorted((notices or {}).items()):
            ib = _norm_iban(ib)
            for p in pids:
                if p not in self.iban[ib]:
                    self.iban[ib].append(p)
        self.children: dict[str, list[str]] = defaultdict(list)
        for pid, rec in sorted(self.cust.items()):
            if rec.get("parent"):
                self.children[rec["parent"]].append(pid)
        self.idx = {"AR": _NameIndex(self.cust), "AP": _NameIndex(self.supp)}
        self.id8: dict[str, list[str]] = {}
        self.alias: dict[str, list[str]] = {}
        if aliases and Path(aliases).exists():
            a = json.loads(Path(aliases).read_text())
            self.id8, self.alias = a.get("id8", {}), a.get("name", {})
        self.boot: dict[tuple[str, str], list[str]] = {}
        self._bc: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))

    # ---- same-phase payer bootstrap (no golden): strong evidence -> payer string -> party ---------
    def payer_keys(self, line: Line) -> list[str]:
        t, r = self.views(line)
        if self.third_party(line) or any(a in t + r for a in AGENCIES + FACTORS):
            return []
        keys = []
        for _, s in self._payer_strings(t, r, fold(line.cpty_name)):
            k = " ".join(name_tokens(s))
            if len(k) >= 3 and k not in keys:
                keys.append(k)
        for m in re.finditer(r"ACH CREDIT (.+?) (?:CCD|PPD|CTX)\b", t):
            k = " ".join(name_tokens(m.group(1)))
            if len(k) >= 3 and k not in keys:
                keys.append(k)
        return keys

    def boot_learn(self, line: Line, pid: str) -> None:
        for k in self.payer_keys(line):
            self._bc[(line.entity, k)][pid] += 1

    def boot_finish(self, min_hits: int = 2, purity: float = 0.9) -> int:
        for key, c in sorted(self._bc.items()):
            tot = sum(c.values())
            pid, n = max(sorted(c.items()), key=lambda x: x[1])
            if tot >= min_hits and n >= purity * tot:
                self.boot[key] = [pid]
        self._bc = defaultdict(lambda: defaultdict(int))
        return len(self.boot)

    # ---- helpers -------------------------------------------------------------------------------
    def _scope(self, line: Line, side: str) -> tuple[str, ...]:
        return AP_POOL.get(line.entity, (line.entity,)) if side == "AP" else (line.entity,)

    def _ok(self, pid: str, side: str, ents: tuple[str, ...]) -> bool:
        rec = (self.cust if side == "AR" else self.supp).get(pid)
        return rec is not None and _entity_of(pid, rec) in ents

    @staticmethod
    def views(line: Line) -> tuple[str, str]:
        t = glue(fold(line.text))
        return t, fold(line.rinfo)

    def third_party(self, line: Line) -> dict | None:
        """{'debtor': name, 'doc': [fragments]} when an agency/confirming/on-behalf marker names the debtor."""
        t, r = self.views(line)
        ms = [m for m in (_EXPTE.search(t), _EXPTE.search(r)) if m]
        if ms:   # the statement text is often truncated; remittance info carries the full EXPTE
            m = max(ms, key=lambda m: (bool(m.group(3)), len(m.group(2))))
            return {"kind": "agency", "debtor": m.group(2), "doc": [m.group(3)] if m.group(3) else []}
        m = _CONF.search(t) or _CONF_OPEN.search(t)
        if m:
            doc = m.group(2).replace(" | ", "").replace(" ", "")
            return {"kind": "confirming", "debtor": m.group(1), "doc": [doc] if doc else []}
        m = _PORCTA.search(t) or _PORCTA.search(r)
        if m:
            return {"kind": "on_behalf", "debtor": m.group(1), "doc": []}
        return None

    def third_party_doc_hint(self, line: Line) -> list[str]:
        tp = self.third_party(line)
        if not tp:
            return []
        out = []
        for d in tp["doc"]:
            a = re.sub(r"[^A-Z0-9]", "", d)
            for v in (d, a, re.sub(r"^[A-Z]+", "", a)):
                if v and len(v) >= 4 and v not in out:
                    out.append(v)
        return out

    def _payer_strings(self, t: str, r: str, cpty: str) -> list[tuple[str, str]]:
        out = []
        for m in _ID8.finditer(t):
            out.append(("n43", m.group(2).strip()))
        for v in (t, r):
            for mk in MARKERS:
                i = v.find(mk)
                if i >= 0:
                    rest = v[i + len(mk):]
                    e = _MARK_END.search(rest)
                    s = (rest[:e.start()] if e else rest).strip()
                    if s:
                        out.append(("marker", s))
        if cpty:
            out.append(("cpty", cpty))
        return out

    # ---- main ----------------------------------------------------------------------------------
    def resolve(self, line: Line, side: str) -> list[tuple[str, str]]:
        ents = self._scope(line, side)
        idx = self.idx[side]
        res: dict[str, str] = {}

        def add(pids, src):
            for p in pids:
                if p not in res and self._ok(p, side, ents):
                    res[p] = src

        t, r = self.views(line)
        tp = self.third_party(line)
        if tp:
            pre = idx.prefix(tp["debtor"], ents)
            add(pre if pre else idx.lookup(tp["debtor"], ents, allow_single=True), "third_party")
            return self._expand(res, side, ents)
        joined = t + " " + r
        is_agency = any(a in joined for a in AGENCIES) or any(f in joined for f in FACTORS)
        if is_agency:
            return []   # the agency/factor is not the party; doc resolution runs party-free
        ib = _norm_iban(line.cpty_acct)
        if ib and not _is_creditor_id(ib):
            add(self.iban.get(ib, ()), "iban")
        for m in _IBAN.finditer(joined):
            ib = m.group(1)
            if not _is_creditor_id(ib) and ib in self.iban:
                add(self.iban[ib], "iban")
        for m in _ID8.finditer(t):
            add(self.id8.get(m.group(1), ()), "id8")
        cpty = fold(line.cpty_name)
        for src, s in self._payer_strings(t, r, cpty):
            key = " ".join(name_tokens(s))
            if key in self.alias:
                add(self.alias[key], "alias")
            pre = idx.prefix(s, ents)
            add(pre if pre else idx.lookup(s, ents), src)
        if not res and self.boot:
            for k in self.payer_keys(line):
                for e in ents:
                    add(self.boot.get((e, k), ()), "boot")
        if not res:
            add(idx.lookup(t, ents), "name")
        return self._expand(res, side, ents)

    def _expand(self, res: dict[str, str], side: str, ents) -> list[tuple[str, str]]:
        out = list(res.items())
        if side == "AR":
            for p, _ in list(out):
                for c in self.children.get(p, ()):
                    if c not in res and self._ok(c, side, ents):
                        res[c] = "child"
                        out.append((c, "child"))
        return out


# ---- learning (TRAIN golden) -------------------------------------------------------------------
def learn_aliases(train_root: Path, until: str, out: Path) -> None:
    """id8 -> customer set and folded payer name -> party set, from 1-party golden item lines."""
    import polars as pl  # IO only
    train_root = Path(train_root)
    bt = pl.read_csv(train_root / "bank_transactions.csv.gz", infer_schema_length=0).filter(
        pl.col("booking_date") <= until).select("bank_txn_id", "text", "remittance_info", "counterparty_name")
    g = pl.read_csv(train_root / "golden_allocations.csv.gz", infer_schema_length=0).filter(
        pl.col("item_id").is_not_null()).select("bank_txn_id", "item_id")
    ar = pl.read_csv(train_root / "ar_invoices.csv.gz", infer_schema_length=0).select(
        "item_id", pl.col("customer_id").alias("p"))
    ap = pl.read_csv(train_root / "ap_invoices.csv.gz", infer_schema_length=0).select(
        "item_id", pl.col("supplier_id").alias("p"))
    cn = pl.read_csv(train_root / "credit_notes.csv.gz", infer_schema_length=0).select(
        "item_id", pl.col("party_id").alias("p"))
    gp = g.join(pl.concat([ar, ap, cn]), on="item_id").group_by("bank_txn_id").agg(pl.col("p").unique())
    gp = gp.filter(pl.col("p").list.len() == 1).with_columns(pl.col("p").list.first())
    df = bt.join(gp, on="bank_txn_id")
    id8: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    nm: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    dummy = Parties.__new__(Parties)
    for text, rinfo, cpty, p in df.select("text", "remittance_info", "counterparty_name", "p").iter_rows():
        t, r = glue(fold(text)), fold(rinfo)
        if _EXPTE.search(t) or _EXPTE.search(r) or _CONF.search(t):
            continue
        for m in _ID8.finditer(t):
            id8[m.group(1)][p] += 1
        for _, s in Parties._payer_strings(dummy, t, r, fold(cpty)):
            k = " ".join(name_tokens(s))
            if k:
                nm[k][p] += 1

    def pure(d):
        o = {}
        for k, c in sorted(d.items()):
            tot = sum(c.values())
            keep = sorted(p for p, n in c.items() if n * 20 >= tot)   # drop <5% noise
            if len(keep) <= MAX_SET:
                o[k] = keep
        return o
    Path(out).parent.mkdir(parents=True, exist_ok=True)
    Path(out).write_text(json.dumps({"until": until, "id8": pure(id8), "name": pure(nm)}, sort_keys=True,
                                    separators=(",", ":")))
