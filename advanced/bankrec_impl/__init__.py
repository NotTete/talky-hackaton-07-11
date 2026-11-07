"""Bank reconciliation at scale — Talky x Anthropic hackathon.

Layout
------
``schema``      closed lists and the submission contract (FORMAT.md)
``config``      TOML settings + env overrides
``data``        phase/masters loading, canonical item table
``normalize``   text / reference / name normalisation helpers
``items``       open-item book (balances, terms, related documents)
``candidates``  blocking: which open items could a bank line settle?
``match``       subset-sum allocation of one bank line over its candidates
``rules``       POLICIES.md: exceptions and no-item categories
``pipeline``    end-to-end run(phase) -> submission CSV
``evaluate``    wraps the official scorer (vendor/bankrec_score.py)
``submit``      presigned S3 upload + live leaderboard flow
"""

__version__ = "0.1.0"
