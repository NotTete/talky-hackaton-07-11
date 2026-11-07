"""The clauses that can change a reconciliation decision (POLICIES.md §3, §5).

A clause is recognised by its **anchor**: the literal phrase that carries the meaning
("anula y sustituye", "annule et remplace", "ersetzt", "a deducir anticipo",
"instalments at", "retención en garantía"). Anchors are exact and per language on
purpose: fuzzy matching on these sentences produces plausible opposites at 0.9 similarity
(`se compensará` vs `será reembolsado`), and a wrong clause is worse than a missed one.

Each clause also carries its **parameters** (percentages, amounts, dates, document
numbers) and, where the sentence has one, its **direction** (offset vs refund, assignee
pays us vs we pay them). Presence alone is never enough: `retention: true` is useless,
`retention: 5% / 8760 / release 2026-06-02` is what a rule needs.
"""

from __future__ import annotations

import re

from .fields import fold, money

#: code -> anchor phrases (lower-case, accent-folded)
ANCHORS: dict[str, tuple[str, ...]] = {
    "instalments": ("instalments at", "instalment", "vencimientos", "en plazos", "raten", "échéances"),
    "retention": ("retención en garantía", "retencion en garantia", "retenue de garantie",
                  "einbehalt", "retention of", "retención del"),
    "advance": ("a deducir anticipo", "deducir anticipo", "anticipo de", "acompte", "anzahlung"),
    "replaces": ("anula y sustituye", "annule et remplace", "ersetzt", "cancels and replaces",
                 "anula y substituye"),
    "iban_change": ("datos bancarios cambian", "new bank details", "neue bankverbindung",
                    "nouvelles coordonnées", "nuevo iban"),
    "assignment": ("cedido a confirming", "cedido a", "assigned to", "abgetreten an", "factoring"),
    "credit_note": ("abono", "avoir", "gutschrift", "credit note", "nota de crédito"),
    "rappel": ("rappel", "bonificación por volumen", "bonificacion por volumen"),
    "service_month": ("correspondiente a", "correspondiente al", "relativo a", "für den monat"),
}

#: direction words (exact, closed list): the same sentence can be either
DIRECTION: dict[str, tuple[str, ...]] = {
    "offset": ("compensar", "compensará", "compensara", "netear", "imputar", "offset", "vortrag"),
    "refund": ("reembols", "devolución", "devolucion", "rembours", "erstatt", "refund"),
}

NEGATION = ("no procede", "no aplica", "sin efecto", "kein", "not applicable", "no se aplica")

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6, "julio": 7,
    "agosto": 8, "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "januar": 1, "februar": 2, "märz": 3, "maerz": 3, "juni": 6, "juli": 7, "oktober": 10,
    "dezember": 12, "janvier": 1, "février": 2, "fevrier": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "août": 8, "aout": 8, "septembre": 9, "octobre": 10,
    "novembre": 11, "décembre": 12, "decembre": 12,
}

DOCNO = re.compile(r"\b([A-Z]{1,4}[-/ ]?\d{2,}(?:[-/]\d+)?)\b")
PCT = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s?%")
AMOUNT = re.compile(r"\d{1,3}(?:[.\u00a0\u202f ]\d{3})*,\d{2}")
DAY_END = re.compile(r"(\d{1,2})[/.-](\d{1,2})[/.-](\d{2,4})")


def _direction(text: str) -> str:
    low = fold(text)
    for name, words in DIRECTION.items():
        if any(word in low for word in words):
            return name
    return ""


def _negated(text: str) -> bool:
    low = fold(text)
    return any(word in low for word in NEGATION)


def _amounts(text: str) -> list[int]:
    out = []
    for raw in AMOUNT.findall(text):
        value = money(raw)
        if value:
            out.append(value)
    return out


def _iso_date(raw: str) -> str:
    match = DAY_END.match(raw)
    if not match:
        return ""
    d, m, y = int(match.group(1)), int(match.group(2)), int(match.group(3))
    if y < 100:
        y += 2000
    return f"{y:04d}-{m:02d}-{d:02d}" if 1 <= m <= 12 and 1 <= d <= 31 else ""


def _service_month(text: str) -> str:
    low = fold(text)
    for name, number in MONTHS.items():
        if name in low:
            year = re.search(r"(20\d{2})", low)
            if year:
                return f"{int(year.group(1)):04d}-{number:02d}"
    return ""


def extract(text: str) -> list[dict]:
    """Clause records for one document's text. Empty list when nothing anchors."""
    out: list[dict] = []
    seen: set[str] = set()
    for code, anchors in ANCHORS.items():
        if code in seen:
            continue
        for line in text.splitlines():
            low = fold(line)
            if not any(anchor in low for anchor in anchors):
                continue
            clause: dict = {"code": code, "span": line.strip()[:200], "negated": _negated(line)}
            if code == "replaces":
                numbers = [n for n in DOCNO.findall(line.upper()) if any(c.isdigit() for c in n)]
                if not numbers:
                    continue  # the anchor without the document it replaces is not actionable
                clause["document"] = numbers[0]
            elif code in {"instalments", "retention", "advance"}:
                pcts = [float(p.replace(",", ".")) for p in PCT.findall(line)]
                amounts = _amounts(line)
                if pcts:
                    clause["pct"] = pcts[0]
                if amounts:
                    clause["amount"] = amounts[0]
                if code == "instalments":
                    count = re.search(r"(\d)\s*(?:instalments|vencimientos|plazos|raten|échéances)", low)
                    clause["count"] = int(count.group(1)) if count else 0
            elif code in {"credit_note", "rappel"}:
                direction = _direction(line)
                if not direction:
                    continue  # presence without direction is not actionable
                clause["direction"] = direction
            elif code == "assignment":
                clause["direction"] = "they_pay" if _direction(line) != "refund" else "we_pay"
            elif code == "service_month":
                month = _service_month(line)
                if not month:
                    continue
                clause["month"] = month
            elif code == "iban_change":
                iban = re.search(r"\b([A-Z]{2}\d{2}[A-Z0-9]{10,30})\b", line.upper())
                if iban:
                    clause["iban"] = iban.group(1)
                effective = DAY_END.search(line)
                if effective:
                    clause["effective"] = _iso_date(effective.group(0))
            clause["confidence"] = 0.9 if not clause["negated"] else 0.4
            out.append(clause)
            seen.add(code)
            break
    return out


def replacement(clauses: list[dict]) -> str:
    """The document number an invoice says it replaces, if any (single, non-negated)."""
    for clause in clauses:
        if clause["code"] == "replaces" and not clause["negated"]:
            return clause.get("document", "")
    return ""


__all__ = ["ANCHORS", "DIRECTION", "MONTHS", "extract", "replacement"]
