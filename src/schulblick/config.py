"""Runtime configuration: environment variables plus the family TOML file."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class ChildConfig:
    first_name: str  # first name as shown in the portal ("Muster, Carlotta Ida" -> "Carlotta")
    display_name: str
    slot: int  # 1..n, used for the colour token


@dataclass(frozen=True)
class UserConfig:
    username: str
    display_name: str
    role: str  # "parent" | "child"
    child: str | None = None  # first_name of the child for role == "child"


@dataclass(frozen=True)
class AISettings:
    enabled: bool = True
    model: str = "claude-haiku-5-5"
    digest_model: str = "claude-haiku-5-5"
    digest_time: str = "06:15"
    max_calls_per_day: int = 150
    include_attendance: bool = False
    secret_path: Path = Path("/run/secrets/anthropic_key")


@dataclass(frozen=True)
class Settings:
    db_path: Path
    config_path: Path
    sph_secret_path: Path
    cookie_secure: bool
    school_id: str
    school_name: str
    tz: ZoneInfo
    fetch_message_bodies: bool
    message_window_days: int
    kids_see_attendance: bool
    homework_stale_days: int
    upcoming_days: int
    exams_horizon_days: int
    events_horizon_days: int
    hidden_event_categories: tuple[str, ...]
    parent_only_keywords: tuple[str, ...]
    children: tuple[ChildConfig, ...]
    users: tuple[UserConfig, ...]
    require_totp_for_parents: bool
    session_days: int
    ai: "AISettings" = None  # type: ignore[assignment]
    extra: dict = field(default_factory=dict)

    def child_by_first_name(self, first_name: str) -> ChildConfig | None:
        for c in self.children:
            if c.first_name.lower() == first_name.lower():
                return c
        return None


def _bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def load_settings(config_path: str | Path | None = None) -> Settings:
    config_path = Path(config_path or os.environ.get("SCHULBLICK_CONFIG", "/config/family.toml"))
    with open(config_path, "rb") as fh:
        raw = tomllib.load(fh)

    school = raw.get("school", {})
    portal = raw.get("portal", {})
    display = raw.get("display", {})
    security = raw.get("security", {})
    ai_raw = raw.get("ai", {})
    ai = AISettings(
        enabled=bool(ai_raw.get("enabled", True)),
        model=str(ai_raw.get("model", "claude-haiku-5-5")),
        digest_model=str(ai_raw.get("digest_model", ai_raw.get("model", "claude-haiku-5-5"))),
        digest_time=str(ai_raw.get("digest_time", "06:15")),
        max_calls_per_day=int(ai_raw.get("max_calls_per_day", 150)),
        include_attendance=bool(ai_raw.get("include_attendance", False)),
        secret_path=Path(os.environ.get("SCHULBLICK_ANTHROPIC_SECRET", "/run/secrets/anthropic_key")),
    )

    children = tuple(
        ChildConfig(
            first_name=c["first_name"],
            display_name=c.get("display_name", c["first_name"]),
            slot=i + 1,
        )
        for i, c in enumerate(raw.get("children", []))
    )
    users = tuple(
        UserConfig(
            username=u["username"].lower(),
            display_name=u.get("display_name", u["username"]),
            role=u["role"],
            child=u.get("child"),
        )
        for u in raw.get("users", [])
    )
    for u in users:
        if u.role not in {"parent", "child"}:
            raise ValueError(f"User {u.username}: role must be 'parent' or 'child'")
        if u.role == "child" and not any(c.first_name == u.child for c in children):
            raise ValueError(f"User {u.username}: child '{u.child}' is not listed under [[children]]")

    return Settings(
        db_path=Path(os.environ.get("SCHULBLICK_DB", "/data/schulblick.db")),
        config_path=config_path,
        sph_secret_path=Path(os.environ.get("SCHULBLICK_SPH_SECRET", "/run/secrets/sph_credentials")),
        cookie_secure=_bool(os.environ.get("SCHULBLICK_COOKIE_SECURE"), True),
        school_id=str(school["id"]),
        school_name=school.get("name", ""),
        tz=ZoneInfo(school.get("timezone", "Europe/Berlin")),
        fetch_message_bodies=bool(portal.get("fetch_message_bodies", True)),
        message_window_days=int(portal.get("message_window_days", 5)),
        kids_see_attendance=bool(display.get("kids_see_attendance", True)),
        homework_stale_days=int(display.get("homework_stale_days", 21)),
        upcoming_days=int(display.get("upcoming_days", 7)),
        exams_horizon_days=int(display.get("exams_horizon_days", 70)),
        events_horizon_days=int(display.get("events_horizon_days", 60)),
        hidden_event_categories=tuple(str(c) for c in display.get("hidden_event_categories", ["15"])),
        parent_only_keywords=tuple(display.get("parent_only_keywords", ["Elternabend", "Förderverein"])),
        children=children,
        users=users,
        require_totp_for_parents=bool(security.get("require_totp_for_parents", True)),
        session_days=int(security.get("session_days", 90)),
        ai=ai,
        extra=raw,
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()


def read_sph_credentials(path: Path) -> tuple[str, str]:
    """Read `username=...` / `password=...` lines from the secret file."""
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().lower()] = value.strip()
    try:
        return values["username"], values["password"]
    except KeyError as exc:  # pragma: no cover - configuration error
        raise ValueError(f"{path} must contain username= and password= lines") from exc
