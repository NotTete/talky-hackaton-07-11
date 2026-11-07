"""The open-item book.

The book holds every invoice / credit note of the phase (plus the previous phases'
open items) with its *running* open amount. Bank lines are applied in booking-date
order, so `PARTIAL`, `DUPLICATE_PAYMENT` and `RETURNED` can be decided from the
state of the book at that moment instead of guessing.

Lookups are vectorised (numpy arrays + integer party codes) so the same code path
works for the 1-month sample and the ~2M-line LIVE/TRAIN phases.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
from datetime import date

import numpy as np
import pandas as pd

from .data import Masters
from .normalize import norm_alnum, ref_variants


def ymd(value: str) -> int:
    """'2025-06-02' -> 20250602 (fast, comparable)."""
    return int(str(value).replace("-", "")[:8] or 0)


@dataclass(slots=True)
class ItemRef:
    """Lightweight snapshot of one open item (what the matcher needs)."""

    pos: int
    item_id: str
    side: str
    kind: str
    entity: str
    party_id: str
    currency: str
    total_cents: int
    open_cents: int
    issue_ymd: int
    due_ymd: int
    terms: str
    document_number: str
    related_document: str


class FXTable:
    """units_per_eur lookups with 'use the last available rate' semantics."""

    def __init__(self, rates: pd.DataFrame):
        self._series: dict[str, tuple[list[int], list[float]]] = {}
        self._eur = 1.0
        if rates.empty:
            return
        df = rates.copy()
        df["ymd"] = df["date"].map(ymd)
        df["rate"] = df["units_per_eur"].astype(float)
        for currency, grp in df.groupby("currency"):
            grp = grp.sort_values("ymd")
            self._series[currency] = (grp["ymd"].tolist(), grp["rate"].tolist())

    def units_per_eur(self, currency: str, on: int) -> float | None:
        if currency == "EUR":
            return self._eur
        series = self._series.get(currency)
        if not series:
            return None
        days, rates = series
        idx = bisect.bisect_right(days, on) - 1
        if idx < 0:
            idx = 0
        return rates[idx]

    def to_eur(self, cents: int, currency: str, on: int) -> float | None:
        rate = self.units_per_eur(currency, on)
        return None if not rate else cents / 100.0 / rate

    def convert(self, cents: int, src: str, dst: str, on: int) -> int | None:
        if src == dst:
            return cents
        eur = self.to_eur(cents, src, on)
        rate = self.units_per_eur(dst, on)
        if eur is None or not rate:
            return None
        return int(round(eur * rate * 100))


class ItemBook:
    def __init__(self, items: pd.DataFrame, masters: Masters):
        if items.empty:
            raise ValueError("no items loaded: check --phase/--item-phases and data/raw")
        df = items.reset_index(drop=True)

        self.n = len(df)
        self.item_ids = df["item_id"].to_numpy(dtype=object)
        self.open_cents = df["open_amount_cents"].to_numpy(dtype=np.int64).copy()
        self.total_cents = df["total_amount_cents"].to_numpy(dtype=np.int64)
        self.currency = df["currency"].to_numpy(dtype=object)
        self.side = df["side"].to_numpy(dtype=object)
        self.kind = df["kind"].to_numpy(dtype=object)
        self.entity = df["entity"].to_numpy(dtype=object)
        self.party_id = df["party_id"].to_numpy(dtype=object)
        self.document_number = df["document_number"].to_numpy(dtype=object)
        self.related_document = df["related_document"].to_numpy(dtype=object)
        self.terms = df["payment_terms"].to_numpy(dtype=object)
        self.issue_ymd = df["issue_date"].map(ymd).to_numpy(dtype=np.int64)
        self.due_ymd = df["due_date"].map(ymd).to_numpy(dtype=np.int64)

        self.masters = masters
        self.fx = FXTable(masters.fx_rates)
        self.today_ymd: int = int(self.issue_ymd.max()) if self.n else 0

        # item_id -> position (sorted arrays instead of a huge dict)
        self._sorted_ids = np.sort(self.item_ids)
        self._sorted_pos = np.argsort(self.item_ids)

        # ---- reference index: normalised document number -> positions
        self.ref_index: dict[str, np.ndarray] = {}
        refs = [self._refs_of(i) for i in range(self.n)]
        flat: dict[str, list[int]] = {}
        for pos, variants in enumerate(refs):
            for variant in variants:
                flat.setdefault(variant, []).append(pos)
        self.ref_index = {k: np.asarray(v, dtype=np.int64) for k, v in flat.items()}

        # ---- party index
        codes, uniques = pd.factorize(self.party_id)
        self.party_code = codes.astype(np.int32)
        self.party_ids = list(uniques)
        self._party_index = {pid: i for i, pid in enumerate(self.party_ids)}

        # group (parent/subsidiary) -> member codes, so "parent pays for subsidiary" works
        self.group_codes: dict[int, np.ndarray] = {}
        for gid, members in masters.group_members.items():
            codes_ = [self._party_index[m] for m in members if m in self._party_index]
            if codes_:
                self.group_codes[self._party_index.get(gid, -1)] = np.asarray(codes_, dtype=np.int32)

        # ---- amount index (open amounts, for amount-only fallback)
        self._amount_order = np.argsort(self.open_cents, kind="stable")
        self._amount_sorted = self.open_cents[self._amount_order]

    # ------------------------------------------------------------- helpers
    def _refs_of(self, pos: int) -> set[str]:
        out: set[str] = set()
        for value in (self.document_number[pos],):
            base = norm_alnum(value)
            if not base:
                continue
            out.add(base)
            out |= ref_variants(value)
            # Our printed numbers vary per entity (FV22-0519421, RE250110905, ...).
            # The trailing digit core survives prefixes and leading zeros.
            digits = "".join(ch for ch in base if ch.isdigit())
            if len(digits) >= 6:
                out.add(digits.lstrip("0") or digits)
        return out

    def position(self, item_id: str) -> int | None:
        idx = int(np.searchsorted(self._sorted_ids, item_id))
        if idx < self.n and self._sorted_ids[idx] == item_id:
            return int(self._sorted_pos[idx])
        return None

    def get(self, pos: int) -> ItemRef:
        return ItemRef(
            pos=pos,
            item_id=self.item_ids[pos],
            side=self.side[pos],
            kind=self.kind[pos],
            entity=self.entity[pos],
            party_id=self.party_id[pos],
            currency=self.currency[pos],
            total_cents=int(self.total_cents[pos]),
            open_cents=int(self.open_cents[pos]),
            issue_ymd=int(self.issue_ymd[pos]),
            due_ymd=int(self.due_ymd[pos]),
            terms=self.terms[pos] or "",
            document_number=self.document_number[pos],
            related_document=self.related_document[pos] or "",
        )

    def party_codes_for(self, party_id: str) -> np.ndarray:
        """The party itself plus its group members (policy §5.1)."""
        code = self._party_index.get(party_id)
        if code is None:
            return np.empty(0, dtype=np.int32)
        members = [code]
        for gid_code, group in self.group_codes.items():
            if gid_code == code or code in group:
                members.extend(int(c) for c in group)
        return np.unique(np.asarray(members, dtype=np.int32))

    # ------------------------------------------------------------ lookups
    def by_refs(self, refs: set[str]) -> tuple[np.ndarray, np.ndarray]:
        """Positions + a confidence score per position from reference hits."""
        scores: dict[int, float] = {}
        for ref in refs:
            for key in (ref, ref.lstrip("0")):
                positions = self.ref_index.get(key)
                if positions is None:
                    continue
                # Shorter/cleaner refs are more trustworthy than long glued tokens.
                weight = 1.0 if len(key) >= 8 else 0.8
                for pos in positions:
                    pos = int(pos)
                    scores[pos] = max(scores.get(pos, 0.0), weight)
        if not scores:
            return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
        pos = np.fromiter(scores.keys(), dtype=np.int64)
        score = np.fromiter(scores.values(), dtype=np.float64)
        order = np.argsort(-score)
        return pos[order], score[order]

    def by_amount(self, cents: int, tol_cents: int, limit: int = 500) -> np.ndarray:
        """Positions whose open amount is within +-tol of `cents` (open > 0)."""
        lo = int(np.searchsorted(self._amount_sorted, cents - tol_cents, "left"))
        hi = int(np.searchsorted(self._amount_sorted, cents + tol_cents, "right"))
        if hi - lo > limit:
            hi = lo + limit
        return self._amount_order[lo:hi]

    def open_positions(self) -> np.ndarray:
        return np.flatnonzero(self.open_cents > 0)

    # ------------------------------------------------------------- updates
    def settle(self, pos: int, cents: int) -> int:
        """Apply `cents` to the item; returns the remaining open amount."""
        applied = min(max(cents, 0), max(int(self.open_cents[pos]), 0))
        self.open_cents[pos] -= applied
        if self.open_cents[pos] < 0:
            self.open_cents[pos] = 0
        return int(self.open_cents[pos])

    def reopen(self, pos: int, cents: int) -> None:
        self.open_cents[pos] = min(int(self.open_cents[pos]) + max(cents, 0), int(self.total_cents[pos]) or cents)


__all__ = ["ItemBook", "ItemRef", "FXTable", "ymd", "date"]
