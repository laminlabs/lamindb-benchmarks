"""Validated configuration for corpus generation."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path

import yaml


@dataclass(frozen=True)
class Config:
    num_studies: int
    num_datasets_per_study: int
    with_index: bool
    seed: int
    table_type: str

    def __post_init__(self) -> None:
        for name in ("num_studies", "num_datasets_per_study", "seed"):
            if type(getattr(self, name)) is not int:
                raise TypeError(f"{name} must be an integer")
        if self.num_studies < 2 or self.num_datasets_per_study < 4:
            raise ValueError("At least 2 studies and 4 datasets per study are required")
        if type(self.with_index) is not bool:
            raise TypeError("with_index must be a YAML boolean (true or false)")
        if self.table_type not in ("csv", "parquet"):
            raise ValueError("table_type must be csv or parquet")

    @classmethod
    def load(cls, path: Path) -> Config:
        try:
            values = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise ValueError(f"Invalid YAML configuration: {error}") from error
        if not isinstance(values, dict):
            raise TypeError("Configuration must be a YAML mapping")
        expected = {field.name for field in fields(cls)}
        if missing := expected - values.keys():
            raise ValueError(f"Missing config fields: {', '.join(sorted(missing))}")
        if extra := values.keys() - expected:
            raise ValueError(
                f"Unknown config fields: {', '.join(sorted(map(str, extra)))}"
            )
        return cls(**values)
