# Board: a union-find (DSU) over parties — critique and verdict

Proposal (owner): use a DSU so that a payer and an item's party match when linked (e.g. a parent paying
for a child), taking into account all payer rules in the organisers' policies. Treasury's proposal is in
`13a_board_dsu_proposal_treasury.md` (identity DSU only; parent → child as a directed, gated map; no
group_id, tax_id or factor edges).

## Accountant reviewer — measured critique (TRAIN May, 2,421 wrong AR lines)
* 75 % (1,814): the golden party is **already** in our payer set → an item-choice problem, not identity.
* 23 % (568): **no party resolved at all** (ar_ref_noise 228, ar_dd_returned 80, ar_dd_batch 50 …).
* 1.6 % (39): a wrong party — and the relation to the right one is "unrelated" in all 39 (no parent,
  child, sibling, same IBAN, trade name or same tax id).
* Upside per edge: parent → child ≈ 0.01–0.03 points; group sibling 0; same IBAN 0; same tax id 0;
  trade name 0 in the wrong-party bucket.
* **Bug found**: children already reached the FIFO / amount / short matchers and were wrong 9 of 12
  times; "exact subset = cash" is not enough for children — require uniqueness and no equal-sum
  alternative in the direct party.
* Against the organisers' rules (§5.1: allocate to the customer paid for, via a parent, a confirming
  bank or an agency): a directed parent → child map gated to a naming marker or an exact reference
  matches the rule; a union does not. §5.2 (payment factory) is an entity-scope rule, not a party edge.

## Verdict
Identity DSU: not worth building (≤ +0.05). Implement the **child gate** (done: children only via exact
reference, unique exact subset, or a marker naming the child). Spend the effort on the **568 no-party
lines** instead — later attacked by the same-phase payer bootstrap and measured with a char n-gram
matcher (which rescued only ~6 %: most of those lines carry no payer name resembling any customer).
