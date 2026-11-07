"""Settings: config/settings.toml + a couple of environment overrides.

Only the paths and the phase live here; heuristics belong in the modules that use
them, so you can see the rule next to the code that applies it.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Repo root — everything is resolved relative to this, not to the current directory.
ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config" / "settings.toml"


@dataclass(slots=True)
class Settings:
    data_dir: Path = ROOT / "data" / "raw"
    submissions_dir: Path = ROOT / "submissions"
    phase: str = "sample"
    # Which item tables to load. sample -> ["sample"]; live -> ["train", "live"].
    item_phases: list[str] = field(default_factory=lambda: ["sample"])

    @property
    def phase_dir(self) -> Path:
        return self.data_dir / self.phase

    def submission_path(self, phase: str | None = None, suffix: str = ".csv.gz") -> Path:
        self.submissions_dir.mkdir(parents=True, exist_ok=True)
        return self.submissions_dir / f"submission_{phase or self.phase}{suffix}"


def load_settings(path: Path | str | None = None) -> Settings:
    """Read the TOML file, then apply env overrides (BANKREC_DATA, BANKREC_PHASE...)."""
    cfg = Path(path or os.environ.get("BANKREC_CONFIG") or DEFAULT_CONFIG)
    raw: dict = {}
    if cfg.exists():
        with cfg.open("rb") as fh:
            raw = tomllib.load(fh)

    paths = raw.get("paths", {})
    run = raw.get("run", {})

    data_dir = Path(paths.get("data_dir", Settings.data_dir))
    if not data_dir.is_absolute():
        data_dir = ROOT / data_dir
    subs = Path(paths.get("submissions_dir", Settings.submissions_dir))
    if not subs.is_absolute():
        subs = ROOT / subs

    settings = Settings(
        data_dir=data_dir,
        submissions_dir=subs,
        phase=run.get("phase", "sample"),
        item_phases=list(run.get("item_phases", ["sample"])),
    )

    if env := os.environ.get("BANKREC_DATA"):
        settings.data_dir = Path(env)
    if env := os.environ.get("BANKREC_PHASE"):
        settings.phase = env
    if env := os.environ.get("BANKREC_ITEM_PHASES"):
        settings.item_phases = [p.strip() for p in env.split(",") if p.strip()]
    return settings
