"""Settings: config/settings.toml + environment overrides.

No external dependency: Python 3.11+ has `tomllib` in the standard library.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "settings.toml"


@dataclass(slots=True)
class Paths:
    data_dir: Path = REPO_ROOT / "data" / "raw"
    submissions_dir: Path = REPO_ROOT / "submissions"
    interim_dir: Path = REPO_ROOT / "data" / "interim"


@dataclass(slots=True)
class Scoring:
    amount_tolerance_pct: float = 0.005
    amount_tolerance_min: float = 0.01
    cash_slack_pct: float = 0.03

    def tolerant(self, golden_cents: int) -> int:
        """Absolute tolerance in cents, mirroring the official scorer."""
        return max(
            int(round(self.amount_tolerance_min * 100)),
            int(round(self.amount_tolerance_pct * abs(golden_cents))),
        )


@dataclass(slots=True)
class Matching:
    max_candidates: int = 60
    max_items_per_txn: int = 14
    subset_search_nodes: int = 200_000
    min_ref_len: int = 5
    amount_only_score: float = 0.35


@dataclass(slots=True)
class Run:
    phase: str = "sample"
    item_phases: list[str] = field(default_factory=lambda: ["sample"])
    limit_txns: int = 0


@dataclass(slots=True)
class FX:
    tolerance_pct: float = 0.03


@dataclass(slots=True)
class Classification:
    default_credit_category: str = "UNIDENTIFIED"
    default_debit_category: str = "BANK_FEES"


@dataclass(slots=True)
class Settings:
    paths: Paths = field(default_factory=Paths)
    run: Run = field(default_factory=Run)
    scoring: Scoring = field(default_factory=Scoring)
    matching: Matching = field(default_factory=Matching)
    fx: FX = field(default_factory=FX)
    classification: Classification = field(default_factory=Classification)

    # ------------------------------------------------------------------ io
    def resolve(self, p: Path | str) -> Path:
        p = Path(p)
        return p if p.is_absolute() else (REPO_ROOT / p)

    @property
    def data_dir(self) -> Path:
        return self.resolve(self.paths.data_dir)

    @property
    def submissions_dir(self) -> Path:
        return self.resolve(self.paths.submissions_dir)

    def submission_path(self, phase: str, suffix: str = ".csv.gz") -> Path:
        self.submissions_dir.mkdir(parents=True, exist_ok=True)
        return self.submissions_dir / f"submission_{phase}{suffix}"


def load_settings(path: Path | str | None = None) -> Settings:
    """Load TOML, then let a few env vars win (handy for `make run`)."""
    cfg_path = Path(path or os.environ.get("BANKREC_CONFIG") or DEFAULT_CONFIG)
    raw: dict = {}
    if cfg_path.exists():
        with cfg_path.open("rb") as fh:
            raw = tomllib.load(fh)

    settings = Settings()
    section_types = {
        "paths": Paths,
        "run": Run,
        "scoring": Scoring,
        "matching": Matching,
        "fx": FX,
        "classification": Classification,
    }
    for name, cls in section_types.items():
        values = raw.get(name, {})
        allowed = {f.name for f in fields(cls)}
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"{cfg_path}: unknown keys in [{name}]: {sorted(unknown)}")
        if name == "paths":
            values = {k: (Path(v) if k in {"data_dir", "submissions_dir", "interim_dir"} else v) for k, v in values.items()}
        setattr(settings, name, cls(**values))

    # Environment overrides (no secrets in the TOML).
    if env := os.environ.get("BANKREC_DATA"):
        settings.paths.data_dir = Path(env)
    if env := os.environ.get("BANKREC_PHASE"):
        settings.run.phase = env
    if env := os.environ.get("BANKREC_ITEM_PHASES"):
        settings.run.item_phases = [p.strip() for p in env.split(",") if p.strip()]
    if env := os.environ.get("BANKREC_LIMIT"):
        settings.run.limit_txns = int(env)
    return settings
