"""ECB reference rates (units per EUR) as Fractions; last published (TARGET) day <= d."""

from __future__ import annotations

import csv
from bisect import bisect_right
from fractions import Fraction
from pathlib import Path

from recon2.model import day


class FX:
    def __init__(self, masters_dir: Path):
        rows: dict[str, list[tuple[int, Fraction]]] = {}
        with open(Path(masters_dir) / "fx_rates.csv", newline="", encoding="utf-8") as fh:
            for r in csv.DictReader(fh):
                v = (r.get("units_per_eur") or "").strip()
                if not v:
                    continue
                rows.setdefault(r["currency"].strip(), []).append((day(r["date"]), Fraction(v)))
        self._days: dict[str, list[int]] = {}
        self._rates: dict[str, list[Fraction]] = {}
        for ccy, lst in rows.items():
            lst.sort()
            self._days[ccy] = [d for d, _ in lst]
            self._rates[ccy] = [r for _, r in lst]

    def rate(self, ccy: str, d: int) -> Fraction:
        if ccy == "EUR" or not ccy:
            return Fraction(1)
        days = self._days.get(ccy)
        if not days:
            raise KeyError(f"no FX rate for {ccy}")
        i = bisect_right(days, d) - 1
        if i < 0:
            i = 0  # before the first published rate: use the earliest
        return self._rates[ccy][i]

    def convert(self, cents: int, src: str, dst: str, d: int) -> int:
        if src == dst:
            return cents
        v = Fraction(cents) * self.rate(dst, d) / self.rate(src, d)
        neg = v < 0
        a = abs(v)
        q = int(a + Fraction(1, 2))  # half-up on magnitude
        return -q if neg else q
