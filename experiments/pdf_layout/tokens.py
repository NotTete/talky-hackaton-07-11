"""Typing tokens: dictionary words vs identifiers vs values.

Why this matters
----------------
A layout is mostly *which kinds of token appear in which column*, not which literal
words. `Montant` vs `Importe` vs `Betrag` are the same role; `113,12` and `1 533,77` are
the same kind. So before comparing layouts we type every chunk:

    DATE | AMOUNT | PCT | QTY | IBAN | BIC | VAT | EMAIL | URL | PHONE |
    DOCNO | CODE | CURRENCY | LABEL | NAME | WORD | OTHER

Two axes are separated on purpose:

* **identifiers** carry digits/uppercase patterns and are almost never reused
  (`FA250222794`, `ES8694022663690810607097`, `PED-42847`);
* **dictionary words** are alphabetic, repeated across the corpus, and are what makes a
  layout legible (`Client`, `Base imponible`, `Conditions`).

Labels get *canonicalised* with a fuzzy match (`difflib`, threshold 0.82) so that
`Base Imponible`, `BASE IMPONIBLE` and `Base imponible:` collapse to one label, and a
typo in one template does not create a new layout class.
"""

from __future__ import annotations

import difflib
import re
import statistics
from collections import Counter

CURRENCIES = {
    "EUR", "USD", "GBP", "CHF", "PLN", "MXN", "BRL", "CNY", "ZAR", "MAD", "CAD", "AUD",
    "SEK", "NOK", "DKK", "TRY", "INR", "JPY", "CZK", "HUF", "RON",
}

RE = {
    "IBAN": re.compile(r"^[A-Z]{2}\d{2}[A-Z0-9]{11,30}$"),
    "BIC": re.compile(r"^[A-Z]{4}[A-Z]{2}[A-Z0-9]{2}([A-Z0-9]{3})?$"),
    "EMAIL": re.compile(r"^[\w.+-]+@[\w-]+\.[\w.]+$"),
    "URL": re.compile(r"^(https?://|www\.)\S+$", re.I),
    "PHONE": re.compile(r"^\+?\d[\d\s().-]{7,}$"),
    "DATE": re.compile(r"^\d{2}[/.]\d{2}[/.]\d{4}$|^\d{4}-\d{2}-\d{2}$"),
    "PCT": re.compile(r"^\d{1,3}(?:[.,]\d{1,2})?\s?%$"),
    "AMOUNT": re.compile(r"^\d{1,3}(?:[.\u00a0\u202f ]\d{3})*,\d{2}$|^\d+\.\d{2}$|^\d{1,3}(?:[.\u00a0\u202f ]\d{3})+$"),
    "QTY": re.compile(r"^\d{1,4}$"),
    "CURRENCY": re.compile(r"^[A-Z]{3}$"),
    "DOCNO": re.compile(r"^(?=.*\d)[A-Z0-9][A-Z0-9./_-]{4,}$"),
    "CODE": re.compile(r"^[A-Z0-9_]{2,8}$"),
    "WORD": re.compile(r"^[^\W\d_][^\W\d_'’.-]*(?:[ '’.-][^\W\d_][^\W\d_'’.-]*)*$", re.UNICODE),
}


def looks_like_value(text: str) -> bool:
    """Could this chunk be the *value* half of a label/value pair? (currency suffix ok)"""
    t = text.strip().rstrip(":;,")
    stripped = re.sub(r"\s*(%)$|\s+([A-Z]{3})$", "", t)
    if stripped != t and stripped.strip() and CURRENCIES & {t.rsplit(None, 1)[-1]}:
        t = stripped.strip()
    return bool(
        RE["DATE"].match(t) or RE["AMOUNT"].match(t) or RE["QTY"].match(t) or RE["DOCNO"].match(t)
        or RE["IBAN"].match(t) or RE["PCT"].match(t)
    )


def classify(token: str, *, labels: set[str] | None = None, vocab: Counter | None = None, names: set[str] | None = None) -> str:
    """Assign one kind to one chunk. Order matters: identifiers before words."""
    t = token.strip().rstrip(":.,;")
    if not t:
        return "OTHER"
    # the layouts almost always suffix the currency: "1 752,08 EUR", "83,61 £", "4 %"
    suffixed = re.fullmatch(r"(.+?)\s+([A-Z]{3}|%)", t)
    if suffixed and (suffixed.group(2) in CURRENCIES or suffixed.group(2) == "%"):
        base = suffixed.group(1)
        if RE["PCT"].match(t) or (RE["AMOUNT"].match(base) or RE["QTY"].match(base)):
            return "AMOUNT"
    if RE["IBAN"].match(t) and len(t) >= 15:
        return "IBAN"
    if RE["EMAIL"].match(t):
        return "EMAIL"
    if RE["URL"].match(t):
        return "URL"
    if RE["DATE"].match(t):
        return "DATE"
    if RE["PCT"].match(t):
        return "PCT"
    if RE["AMOUNT"].match(t):
        return "AMOUNT"
    if t in CURRENCIES:
        return "CURRENCY"
    if RE["BIC"].match(t) and len(t) in (8, 11) and t not in CURRENCIES:
        return "BIC"
    if RE["PHONE"].match(t) and len(re.sub(r"\D", "", t)) >= 9:
        return "PHONE"
    if RE["QTY"].match(t):
        return "QTY"
    if labels and t.lower() in labels:
        return "LABEL"
    if RE["DOCNO"].match(t) and re.search(r"\d", t) and len(t) >= 5:
        return "DOCNO"
    if re.match(r"^[A-Z]{2}\d{8,12}$", t):
        return "VAT"
    if RE["CODE"].match(t) and re.search(r"\d", t):
        return "CODE"
    if RE["WORD"].match(t):
        if names and t.lower() in names:
            return "NAME"
        if len(t.split()) >= 2:
            return "TEXT"          # product / service description
        if vocab is not None and vocab.get(t.lower(), 0) >= 3:
            return "WORD"
        return "WORD" if t[:1].isupper() else "OTHER"
    if re.search(r"[^\W\d_]{3,}", t) and len(t.split()) >= 2:
        return "TEXT"              # "Chlorine tablets 50 lb"
    return "OTHER"


def _page_texts(page) -> list[str]:
    """Works on both Page objects (extract.py) and the JSON form (run.py)."""
    if isinstance(page, dict):
        return [line.get("text", "") for line in page.get("lines", [])]
    return [line.text for line in page.lines]


def build_dictionaries(docs, *, min_freq: int = 3) -> tuple[Counter, set[str]]:
    """Corpus vocabulary (alphabetic chunks) and the label set seen in label/value pairs."""
    vocab: Counter = Counter()
    labels: Counter = Counter()
    for doc in docs:
        for page in doc.pages:
            texts = _page_texts(page)
            for text in texts:
                for word in re.findall(r"[^\W\d_]{2,}", text, re.UNICODE):
                    vocab[word.lower()] += 1
        for label, _value in doc.pairs:
            labels[label.strip().lower()] += 1
    # a label must recur: a one-off phrase is a sentence, not a field name
    label_set = {label for label, n in labels.items() if n >= 2 and len(label) <= 40}
    return Counter({w: n for w, n in vocab.items() if n >= min_freq}), label_set


def canonical_labels(pairs: list[tuple[str, str]], label_set: set[str], threshold: float = 0.82) -> dict[str, str]:
    """Fuzzy-merge label variants into one canonical spelling (most frequent wins)."""
    counts = Counter(lbl.strip().lower() for lbl, _ in pairs)
    ordered = [lbl for lbl, _ in counts.most_common()]
    canon: dict[str, str] = {}
    for label in ordered:
        if label in canon:
            continue
        canon[label] = label
        for other in ordered:
            if other in canon:
                continue
            if difflib.SequenceMatcher(None, label, other).ratio() >= threshold:
                canon[other] = label
    return canon


def name_dictionary(masters) -> set[str]:
    """Lower-cased first tokens of customers/suppliers: used to spot party names."""
    names: set[str] = set()
    for df, col in ((masters.customers, "name"), (masters.suppliers, "name")):
        if df.is_empty():
            continue
        for value in df[col].to_list():
            for word in re.findall(r"[^\W\d_]{3,}", (value or "").lower(), re.UNICODE):
                names.add(word)
    return names


def identifier_ratio(kinds: list[str]) -> float:
    ids = {"DOCNO", "CODE", "IBAN", "BIC", "VAT", "EMAIL", "URL", "PHONE"}
    return sum(k in ids for k in kinds) / len(kinds) if kinds else 0.0


def entropy(values: list[str]) -> float:
    """Shannon entropy over exact values: identifiers are high, labels repeat."""
    if not values:
        return 0.0
    counts = Counter(values)
    total = len(values)
    return -sum((n / total) * __import__("math").log2(n / total) for n in counts.values())


def summarise(kinds: list[str]) -> dict[str, float]:
    counts = Counter(kinds)
    total = sum(counts.values()) or 1
    return {k: round(n / total, 4) for k, n in counts.most_common()}


__all__ = [
    "CURRENCIES",
    "RE",
    "build_dictionaries",
    "canonical_labels",
    "classify",
    "entropy",
    "identifier_ratio",
    "looks_like_value",
    "name_dictionary",
    "statistics",
    "summarise",
]
