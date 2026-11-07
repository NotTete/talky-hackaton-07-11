"""Talky x Anthropic — bank reconciliation at scale.

Load the dumps, resolve every bank line to open items, write a valid submission, score
it locally. Grow it one rule at a time (see README.md).

    config.py    settings.toml + env overrides
    schema.py    the contract: closed lists, limits, tolerance, score weights
    data.py      reading the dumps into canonical tables (money as integer cents)
    text.py      normalising references and party names
    alt/         the engine: classify the line, read its answer key, allocate the cash
    pdf/         PDF -> facts (evidence for the engine, never decisions)
    preprocess.py  batch / advice / card / cheque context for the build report
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
