"""ECB reference rates (units per EUR) as Fractions; last published (TARGET) day <= d."""

from __future__ import annotations

import csv
import sys
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
        self.missing: dict[str, int] = {}   # ccy -> lookups served without any published rate
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
            # Loud but non-crashing (FINAL brings ZAR; masters_final should carry it): rate 1 is
            # wrong, but every comparison involving it is then a plain unit comparison.
            # A wrong rate (e.g. 1 ZAR = 1 EUR, really ~19.5) would create false cross-currency
            # matches: refuse to convert instead. Callers treat KeyError as "no conversion".
            if ccy not in self.missing:
                print(f"[recon2] WARNING: no FX rate for {ccy}; cross-currency matches against it are skipped", file=sys.stderr, flush=True)
            self.missing[ccy] = self.missing.get(ccy, 0) + 1
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
