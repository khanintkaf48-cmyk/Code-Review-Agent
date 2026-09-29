from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_path() -> Path:
    return Path(os.getenv("REVIEWER_HOME", ".reviewer"))


@dataclass
class Config:
    model: str = field(default_factory=lambda: os.getenv("REVIEWER_MODEL", "claude-sonnet-5-5"))
    data_dir: Path = field(default_factory=_env_path)
    max_diff_chars: int = 60_000
    max_tokens: int = 6_000
    # A pattern is silenced once the team rejected it this many times
    # (and rejections outnumber acceptances).
    suppress_after_rejects: int = 2
    # Findings of these severities are NEVER hidden, even if the team rejected them before.
    # (A wrongly rejected SQL injection must not stay silent forever.)
    never_suppress_severities: tuple = ("blocker",)
    # How much memory context is injected into every review prompt.
    max_rules_in_prompt: int = 40
    max_mistakes_in_prompt: int = 10
    max_pushback_in_prompt: int = 15

    @property
    def db_path(self) -> Path:
        return self.data_dir / "memory.db"

    @property
    def standards_path(self) -> Path:
        """Optional hand-written standards (markdown), committed to the repo."""
        return self.data_dir / "standards.md"
