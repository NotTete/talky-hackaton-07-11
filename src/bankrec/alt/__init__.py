"""The `alt` engine: operation-first reconciliation.

Where `bankrec.matching` asks "which items could this line settle?" and then checks
whether a subset adds up, this engine asks a different question first: **what kind of
banking operation is this line?** A direct-debit batch credit, a card settlement, an
aggregated payment run, a returned collection and a plain transfer are five different
objects with five different answer keys, and four of those keys are *documents on disk*
rather than anything you can infer from the cash amount.

So the flow is: classify -> look the operation's document up -> expand it into items ->
explain the cash difference. Amount-based candidate ranking is the last resort, not the
first move.
"""

from importlib import import_module


def __getattr__(name):  # lazy: sub-modules are importable before the engine exists
    if name == "Engine":
        return import_module(".engine", __name__).Engine
    raise AttributeError(name)

__all__ = ["Engine"]
