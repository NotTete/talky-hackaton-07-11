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


def load_dotenv(path: Path | None = None, override: bool = False) -> Path | None:
    """Read KEY=VALUE lines from .env into os.environ.

    `.env.example` says to copy it to `.env` and that the Makefile and the CLI read it,
    so this is the code that makes that true. The shell still wins by default, so an
    exported HK_TOKEN is never silently replaced by a stale file. Values are stripped of
    the quotes a hand-edited file usually leaves around them.
    """
    env = Path(path or os.environ.get("BANKREC_ENV") or ROOT / ".env")
    if not env.exists():
        return None
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[7:].strip()
        value = value.strip()
        while len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and (override or key not in os.environ):
            os.environ[key] = value
    return env


@dataclass(slots=True)
class Settings:
    data_dir: Path = ROOT / "data"
    builds_dir: Path = ROOT / "builds"
    phase: str = "sample"
    # Which item tables to load. sample -> ["sample"]; live -> ["train", "live"].
    item_phases: list[str] = field(default_factory=lambda: ["sample"])
    # Use the PDF facts cache as extra evidence in the engine (`bankrec pdf-facts`).
    # Off by default: facts can only be trusted once they have been built and inspected.
    use_pdf_facts: bool = True

    @property
    def phase_dir(self) -> Path:
        return self.data_dir / self.phase


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
    builds = Path(paths.get("builds_dir", Settings.builds_dir))
    if not builds.is_absolute():
        builds = ROOT / builds

    settings = Settings(
        data_dir=data_dir,
        builds_dir=builds,
        phase=run.get("phase", "sample"),
        item_phases=list(run.get("item_phases", ["sample"])),
        use_pdf_facts=bool(run.get("use_pdf_facts", True)),
    )

    if env := os.environ.get("BANKREC_DATA"):
        settings.data_dir = Path(env)
    if env := os.environ.get("BANKREC_PHASE"):
        settings.phase = env
    if env := os.environ.get("BANKREC_ITEM_PHASES"):
        settings.item_phases = [p.strip() for p in env.split(",") if p.strip()]
    return settings


load_dotenv()  # once, at import: same environment for the CLI, the Makefile and scripts
