"""Deterministic char 3-gram TF-IDF payer matcher (pure Python, no randomness).

match(payer, entity, side) -> [(party_id, score)] sorted by (-score, party_id), score in [0,1].
Pools are per (side, entity); `side_entities` maps an entity to the entities whose parties it
may pay (e.g. {"ES00": ("ES00","ES01","ES02","PT01")}).  Blocking: postings of 3-grams whose
document frequency is <= RARE_DF (common grams only rescore, never propose).
"""
from __future__ import annotations

import math
import re
from collections import defaultdict

from recon2.textviews import fold

_LEGAL = re.compile(
    r"\b(S\.? ?A\.? DE C\.? ?V\.?|S\.? ?R\.? ?L\.? ?U?\.?|S\.? ?L\.? ?U?\.?|S\.? ?A\.? ?S?\.?U?\.?|S\.? ?A\.? ?R\.? ?L\.?|"
    r"E\.? ?U\.? ?R\.? ?L\.?|G\.? ?M\.? ?B\.? ?H\.?|B\.? ?V\.?|N\.? ?V\.?|P\.? ?T\.? ?Y\.?|L\.? ?T\.? ?D\.? ?A?\.?|"
    r"L\.? ?L\.? ?C\.?|S\.? ?P\.? ?A\.?|INC\.?|CORP\.?|CO\.?|LIMITED|LIMITADA|S\.? ?C\.? ?P\.?|SRLS|SASU|KG|AG|OHG|"
    r"PLC|BHD|PTE|EIRELI|LDA|ZOO|CV|SL|SA|SLU|SAS|SARL|SRL|SPA|LTD|LLC|GMBH|BV|PTY|EURL|LTDA)(?=\s|$|\.)")
_NON = re.compile(r"[^A-Z0-9]+")
RARE_DF = 60
TOP_EXACT = 30


def norm(s: str) -> str:
    t = fold(s)
    prev = None
    while prev != t:
        prev = t
        t = _LEGAL.sub(" ", t)
    return _NON.sub(" ", t).strip()


def grams(t: str) -> list[str]:
    p = f" {t} "
    return [p[i:i + 3] for i in range(len(p) - 2)] if len(t) >= 2 else []


def initials(t: str) -> str:
    return "".join(w[0] for w in t.split() if not w.isdigit())


class PayerMatcher:
    def __init__(self, masters, side_entities: dict | None = None):
        self.m = masters
        self.side_entities = side_entities or {}
        self.built = False

    def build(self) -> "PayerMatcher":
        names: dict[tuple, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        for side, tab in (("AR", self.m.customers), ("AP", self.m.suppliers)):
            for pid, rec in tab.items():
                ent = rec.get("entity") or pid[1:5]
                for f in ("name", "trade_name"):
                    n = norm(rec.get(f) or "")
                    if len(n) >= 2:
                        names[(side, ent)][n].add(pid)
        self.pool = {}
        for key, nm in names.items():
            keys = sorted(nm)
            df: dict[str, int] = defaultdict(int)
            tfs = []
            for n in keys:
                tf: dict[str, int] = defaultdict(int)
                for g in grams(n):
                    tf[g] += 1
                tfs.append(tf)
                for g in tf:
                    df[g] += 1
            N = len(keys)
            idf = {g: math.log(1 + N / d) for g, d in df.items()}
            vecs, post, ini = [], defaultdict(list), defaultdict(set)
            for i, (n, tf) in enumerate(zip(keys, tfs)):
                v = {g: (1 + math.log(c)) * idf[g] for g, c in tf.items()}
                z = math.sqrt(sum(x * x for x in v.values())) or 1.0
                v = {g: x / z for g, x in v.items()}
                vecs.append(v)
                for g in v:
                    if df[g] <= RARE_DF:
                        post[g].append(i)
                ini[initials(n)].update(nm[n])
            self.pool[key] = (keys, [sorted(nm[n]) for n in keys], vecs, post, idf, ini)
        self.built = True
        return self

    def _pools(self, entity, side):
        if not entity:
            return [k for k in sorted(self.pool) if k[0] == side]
        ents = self.side_entities.get(entity) or (entity,)
        return [(side, e) for e in ents if (side, e) in self.pool]

    def match(self, payer_string: str, entity: str, side: str, k: int = 5) -> list[tuple[str, float]]:
        if not self.built:
            self.build()
        q = norm(payer_string)
        if len(q) < 2:
            return []
        qg: dict[str, int] = defaultdict(int)
        for g in grams(q):
            qg[g] += 1
        qi = _NON.sub("", fold(payer_string))
        best: dict[str, float] = {}
        for key in self._pools(entity, side):
            keys, pids, vecs, post, idf, ini = self.pool[key]
            qv = {g: (1 + math.log(c)) * idf[g] for g, c in qg.items() if g in idf}
            if qv:
                z = math.sqrt(sum(x * x for x in qv.values())) or 1.0
                acc: dict[int, float] = defaultdict(float)
                for g, w in qv.items():
                    for i in post.get(g, ()):
                        acc[i] += w * vecs[i][g]
                for i in sorted(acc, key=lambda i: (-acc[i], i))[:TOP_EXACT]:
                    s = sum(w * vecs[i].get(g, 0.0) for g, w in qv.items()) / z
                    for pid in pids[i]:
                        if s > best.get(pid, 0.0):
                            best[pid] = s
            if 2 <= len(qi) <= 5 and qi.isalpha():          # initials channel ('B E', 'S.L.S.')
                hit = ini.get(qi)
                if hit and len(hit) <= 3:
                    for pid in hit:
                        best[pid] = max(best.get(pid, 0.0), 0.5 / len(hit))
        out = sorted(((p, round(s, 6)) for p, s in best.items()), key=lambda x: (-x[1], x[0]))
        return out[:k]
