"""Talky x Anthropic — bank reconciliation at scale.

A deliberately small skeleton: load the dumps, match bank lines to open items, write
a valid submission, score it locally. Grow it one rule at a time (see README.md).

    config.py    settings.toml + env overrides
    schema.py    the contract: closed lists, limits, tolerance, score weights
    data.py      reading the dumps into canonical tables (money as integer cents)
    text.py      normalising references and party names
    matching.py  <- the part to build: which items does a bank line settle
    rules.py     <- the part to build: exception / category codes (POLICIES.md)
    pipeline.py  phase -> submission CSV
    evaluate.py  official scorer wrapper (vendor/bankrec_score.py)
    insights.py  golden vs submission diff: the `--explain` report
    hooks.py     observe-only per-line decision hooks (`process --trace`)
    experiments.py  provenance, run list and `compare` (no tracking server)
    stages.py       preprocessing runs: metrics + report files + provenance
    documents.py    preprocessing report: parsed documents, integrity, attachment
    submit.py    presigned S3 upload + leaderboard flow + the human confirmation gate
    cli.py       python -m bankrec run|score|validate|submit|doctor
"""

__version__ = "0.1.0"
