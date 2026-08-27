"""What every fetch strategy must provide."""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any

from app.linkedin.client import LinkedInClient
from app.linkedin.urls import ProfileRef
from app.models import Profile


@dataclass
class StrategyResult:
    name: str
    profile: Profile
    raw: dict[str, Any] | None = None
    warnings: list[str] = field(default_factory=list)


class Strategy(abc.ABC):
    """One way to read a profile.

    Strategies are ordered by yield, not by elegance. The orchestrator walks
    them and stops as soon as one returns a profile that is complete enough.
    """

    name: str = "unnamed"
    needs_auth: bool = True
    description: str = ""

    @abc.abstractmethod
    async def fetch(self, client: LinkedInClient, ref: ProfileRef) -> StrategyResult:
        """Return a profile or raise a LinkedInAPIError."""
