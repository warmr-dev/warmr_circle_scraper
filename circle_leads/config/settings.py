"""Configuration loading. All lead requirements live in YAML, not in code."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

DEFAULT_CONFIG_PATH = Path(__file__).parent / "requirements.yaml"

# Content the system never collects, whatever a token would permit. Kept in one
# place so a permission file and requirements.yaml cannot drift apart.
DEFAULT_EXCLUDED_CONTENT = (
    "direct_messages",
    "member_bios",
    "email_addresses",
    "phone_numbers",
)


class ScoringWeights(BaseModel):
    hiring_intent: int = 40
    target_role_match: int = 20
    target_skill_match: int = 15
    budget_mentioned: int = 10
    company_identified: int = 5
    recent_post: int = 10
    recency_days: int = 7


class PriorityThresholds(BaseModel):
    high: int = 80
    medium: int = 50


class Keywords(BaseModel):
    include: list[str] = Field(default_factory=list)
    exclude: list[str] = Field(default_factory=list)


class RateLimitConfig(BaseModel):
    requests_per_minute: int = 60
    max_retries: int = 5
    backoff_base_seconds: float = 2.0
    max_backoff_seconds: float = 60.0


class Requirements(BaseModel):
    """The user-editable lead specification."""

    target_roles: list[str] = Field(default_factory=list)
    target_skills: list[str] = Field(default_factory=list)
    exclude_job_seekers: bool = True
    minimum_confidence: float = 0.80
    llm_escalation_threshold: int = 55
    keywords: Keywords = Field(default_factory=Keywords)
    scoring: ScoringWeights = Field(default_factory=ScoringWeights)
    priority_thresholds: PriorityThresholds = Field(default_factory=PriorityThresholds)
    excluded_content: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_CONTENT))
    retention_days: int = 30
    rate_limit: RateLimitConfig = Field(default_factory=RateLimitConfig)

    @field_validator("minimum_confidence")
    @classmethod
    def _confidence_range(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError("minimum_confidence must be between 0 and 1")
        return v

    @property
    def roles_lower(self) -> list[str]:
        return [r.lower() for r in self.target_roles]

    @property
    def skills_lower(self) -> list[str]:
        return [s.lower() for s in self.target_skills]

    def priority_for(self, score: int) -> str:
        if score >= self.priority_thresholds.high:
            return "HIGH"
        if score >= self.priority_thresholds.medium:
            return "MEDIUM"
        return "LOW"


def load_requirements(path: str | Path | None = None) -> Requirements:
    """Load requirements from YAML, falling back to the packaged defaults."""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not cfg_path.exists():
        raise FileNotFoundError(f"Requirements file not found: {cfg_path}")
    data: dict[str, Any] = yaml.safe_load(cfg_path.read_text()) or {}
    return Requirements(**data)


class CommunityPermission(BaseModel):
    """Per-community authorization record. One file per community.

    Mirrors the consent contract: nothing is collected beyond the spaces and
    rooms the operator named, and nothing at all unless status is `approved`.
    """

    community_id: str
    community_url: str | None = None
    permission_status: str = "candidate"
    approved_purpose: str | None = None
    ingestion_route: str = "admin_api_v2"
    allowed_space_ids: list[str] = Field(default_factory=list)
    allowed_chat_room_uuids: list[str] = Field(default_factory=list)
    excluded_content: list[str] = Field(default_factory=lambda: list(DEFAULT_EXCLUDED_CONTENT))
    retention_days: int = 30
    outreach_mode: str = "reply_in_original_thread"
    operator_contact: str | None = None
    approval_reference: str | None = None

    # Names of env vars holding secrets. The secrets themselves are never
    # stored in this file — only the variable names that point at them.
    admin_token_env: str | None = None
    headless_auth_token_env: str | None = None
    member_id_env: str | None = None

    @property
    def is_approved(self) -> bool:
        return self.permission_status == "approved"

    def secret(self, env_name: str | None) -> str | None:
        """Read a secret from the environment. Never logged, never persisted."""
        if not env_name:
            return None
        return os.environ.get(env_name) or None


def load_community_permissions(directory: str | Path) -> list[CommunityPermission]:
    """Load every community permission file in a directory."""
    d = Path(directory)
    if not d.exists():
        return []
    out: list[CommunityPermission] = []
    for f in sorted(list(d.glob("*.yaml")) + list(d.glob("*.yml"))):
        data = yaml.safe_load(f.read_text()) or {}
        out.append(CommunityPermission(**data))
    return out
