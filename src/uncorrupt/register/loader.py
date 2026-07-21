"""Load + validate sources/ and locales/. Every pipeline run resolves its source's
entry or refuses to run (ADR-001 D5 / the connector contract)."""

from __future__ import annotations

from pathlib import Path

import yaml

from uncorrupt.core.errors import RegisterError
from uncorrupt.register.models import LocaleProfile, SourceEntry

_ROOT = Path(__file__).resolve().parents[3]
SOURCES_DIR = _ROOT / "sources"
LOCALES_DIR = _ROOT / "locales"


def load_source(source_id: str, *, sources_dir: Path = SOURCES_DIR) -> SourceEntry:
    path = sources_dir / f"{source_id}.yml"
    if not path.exists():
        raise RegisterError(
            f"no register entry sources/{source_id}.yml — connector may not run"
        )
    return SourceEntry.model_validate(yaml.safe_load(path.read_text()))


def load_locale(code: str, *, locales_dir: Path = LOCALES_DIR) -> LocaleProfile:
    path = locales_dir / f"{code.lower()}.yml"
    if not path.exists():
        raise RegisterError(f"no locale profile locales/{code.lower()}.yml")
    return LocaleProfile.model_validate(yaml.safe_load(path.read_text()))


def all_sources(*, sources_dir: Path = SOURCES_DIR) -> list[SourceEntry]:
    return [
        SourceEntry.model_validate(yaml.safe_load(p.read_text()))
        for p in sorted(sources_dir.glob("*.yml"))
        if not p.name.startswith("_")
    ]
