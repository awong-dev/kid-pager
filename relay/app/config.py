"""Runtime configuration from environment variables (see relay/.env.example).

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Settings:

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
        )
