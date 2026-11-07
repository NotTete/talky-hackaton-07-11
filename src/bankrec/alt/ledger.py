"""The item book: what is open, who owes it, and which strings name it.

One object owns three responsibilities that the rest of the engine only ever reads
through narrow methods:

* **identity** — every spelling a document number can arrive in (exact, digits-only,
  leading zeros stripped, and *prefix* for bank-truncated text) maps to its items;
* **ownership** — a paying IBAN or a party name maps to a party, and a party maps to its
  open items, because half the lines carry no usable reference at all (POLICIES.md §5.5);
* **state** — balances are decremented as lines are processed in booking order, which is
  the only reason `PARTIAL`, `DUPLICATE_PAYMENT` and `RETURNED` are decidable at all
  (CLAUDE.md §7).

Everything O(items) is indexed once in `__init__`; nothing in the per-line path rescans
the ledger (CLAUDE.md §11).
"""

from __future__ import annotations

import bisect
from collections import defaultdict

from ..data import Masters
from ..text import alnum, name_similarity, name_tokens
from .refs import digits, variants

# A prefix query below this length matches half the ledger; it is noise, not evidence.
MIN_PREFIX = 7
# Above this many candidates a prefix/party query stops being evidence (CLAUDE.md §11b).
MAX_FANOUT = 400


def _ymd(value: str) -> int:
    d = str(value or "").replace("-", "")[:8]
    return int(d) if d.isdigit() else 0


class Ledger:
    def __init__(self, items, masters: Masters):
        self.masters = masters
        self.items: dict[str, dict] = {}
        self.open: dict[str, int] = {}
        self.paid: dict[str, int] = {}

        by_doc: dict[str, list[str]] = defaultdict(list)
        by_party: dict[str, list[str]] = defaultdict(list)
        by_amount: dict[int, list[str]] = defaultdict(list)

        rows = items.iter_rows(named=True) if hasattr(items, "iter_rows") else items
        for row in rows:
            item_id = row["item_id"]
            row["issue_ymd"] = _ymd(row.get("issue_date"))
            row["due_ymd"] = _ymd(row.get("due_date"))
            row["doc_alnum"] = alnum(row.get("document_number"))
            self.items[item_id] = row
            self.open[item_id] = int(row["open_cents"])
            self.paid[item_id] = 0

            for key in variants(row.get("document_number") or ""):
                by_doc[key].append(item_id)
            po = alnum(row.get("po_reference") or "")
            if len(po) >= 6:
                by_doc[po].append(item_id)
            party = row.get("party_id") or ""
            if party:
                by_party[party].append(item_id)
            by_amount[int(row["total_cents"])].append(item_id)

        self._by_doc = dict(by_doc)
        self._by_party = dict(by_party)
        self._by_amount = dict(by_amount)
        self._amounts = sorted(self._by_amount)
        # Prefix queries: one sorted key list, bisect for the [token, token+1) range.
        self._doc_keys = sorted(self._by_doc)

        # Items of a party, oldest first — FIFO is the documented tie-break (§5.6).
        for ids in self._by_party.values():
            ids.sort(key=lambda i: (self.items[i]["issue_ymd"], i))

        self._party_tokens: dict[str, list[str]] = defaultdict(list)
        for party_id, name in masters.party_names.items():
            for token in sorted(name_tokens(name)):
                if len(token) >= 4:
                    self._party_tokens[token].append(party_id)

    # ------------------------------------------------------------- identity
    def by_doc(self, token: str) -> list[str]:
        """Items whose document number is spelled `token` (exact, or digits-only)."""
        for key in variants(token):
            hit = self._by_doc.get(key)
            if hit:
                return hit
        return []

    def by_doc_prefix(self, token: str) -> list[str]:
        """Items whose document number *starts with* `token` (bank truncation, §5.5)."""
        key = alnum(token)
        if len(key) < MIN_PREFIX:
            return []
        lo = bisect.bisect_left(self._doc_keys, key)
        out: list[str] = []
        for candidate in self._doc_keys[lo : lo + 64]:
            if not candidate.startswith(key):
                break
            out.extend(self._by_doc[candidate])
            if len(out) > MAX_FANOUT:
                return []
        return out

    def by_doc_fuzzy(self, token: str, scope: list[str]) -> list[str]:
        """Items in `scope` whose document number is one transposition/typo away.

        Only ever called with a scope (a party's items, an advice's documents): a global
        edit-distance search over 65k documents would be both slow and wrong.
        """
        core = digits(alnum(token))
        if len(core) < 6:
            return []
        out = []
        for item_id in scope:
            other = digits(self.items[item_id]["doc_alnum"])
            if len(other) == len(core) and _within_one_swap(core, other):
                out.append(item_id)
        return out

    # ------------------------------------------------------------ ownership
    def party_for_iban(self, iban: str) -> tuple[str, str] | None:
        return self.masters.iban_owner.get(alnum(iban))

    def party_for_name(self, text: str) -> str:
        """Best party id for a (possibly truncated) counterparty name, or ''."""
        tokens = name_tokens(text)
        if not tokens:
            return ""
        votes: dict[str, int] = defaultdict(int)
        for token in tokens:
            for party_id in self._party_tokens.get(token, ()):
                votes[party_id] += 1
        if not votes:
            return ""
        best, best_key = "", (0.0, 0, "")
        for party_id, hits in votes.items():
            similarity = name_similarity(tokens, name_tokens(self.masters.party_names.get(party_id, "")))
            key = (similarity, hits, party_id)
            if key > best_key:
                best, best_key = party_id, key
        return best if best_key[0] >= 0.6 else ""

    def party_items(self, party_id: str) -> list[str]:
        return self._by_party.get(party_id, [])

    def by_amount(self, cents: int, tol: int) -> list[str]:
        lo = bisect.bisect_left(self._amounts, cents - tol)
        hi = bisect.bisect_right(self._amounts, cents + tol)
        out: list[str] = []
        for amount in self._amounts[lo:hi]:
            out.extend(self._by_amount[amount])
            if len(out) > MAX_FANOUT:
                return []
        return out

    # ---------------------------------------------------------------- state
    def item(self, item_id: str) -> dict:
        return self.items[item_id]

    def open_cents(self, item_id: str) -> int:
        return self.open.get(item_id, 0)

    def is_settled(self, item_id: str) -> bool:
        """True once the item has been fully cleared by an earlier line (§7)."""
        return self.paid.get(item_id, 0) > 0 and self.open.get(item_id, 0) <= 0

    def apply(self, item_id: str, cents: int) -> None:
        self.open[item_id] = self.open.get(item_id, 0) - cents
        self.paid[item_id] = self.paid.get(item_id, 0) + cents

    def reopen(self, item_id: str, cents: int) -> None:
        self.open[item_id] = self.open.get(item_id, 0) + cents
        self.paid[item_id] = max(self.paid.get(item_id, 0) - cents, 0)

    def __len__(self) -> int:
        return len(self.items)


def _within_one_swap(a: str, b: str) -> bool:
    """Equal length and differing in at most one adjacent transposition or one digit."""
    diff = [i for i, (x, y) in enumerate(zip(a, b, strict=True)) if x != y]
    if len(diff) == 1:
        return True
    if len(diff) == 2 and diff[1] == diff[0] + 1:
        i, j = diff
        return a[i] == b[j] and a[j] == b[i]
    return False


__all__ = ["Ledger"]
