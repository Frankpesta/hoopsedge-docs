"""The one versioned config document (brief §8.2, §10; D98), stored as DynamoDB `CONFIG/CURRENT`.

It is the v2 config (thresholds, line modes, tickets, `dry_run`) plus what the jobs need besides:
schedule times, `pause_all` and the proxy flag. Slate runs, the scheduler and the API all read this
one document, so a change made on the dashboard is what the next run uses. Every save is a new
version, kept as `CONFIG/v#NNNNNN` (the API writes them).

A document saved before v2 (no `sports`) loads as the defaults with its schedules and switches
carried over, so an upgrade never loses the owner's Dry run or pause choice.
"""

import re
from decimal import Decimal
from typing import Any

from pydantic import ConfigDict, field_validator

from hoopsedge.common.config_v2 import V2Config, load_defaults
from hoopsedge.pipeline.schedule import BACKUP_TIME, SCORES_TIME, SLATES, SPORTS

CLOCK = r"^([01]\d|2[0-3]):[0-5]\d$"


def default_schedules() -> dict[str, str]:
    times = {"backup": BACKUP_TIME, **{f"{s.lower()}_scores": SCORES_TIME for s in SPORTS}}
    times |= {name: at for name, (_, at, _) in SLATES.items()}
    return {name: at.strftime("%H:%M") for name, at in times.items()}


SCHEDULE_NAMES = tuple(default_schedules())


class ConfigDocument(V2Config):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schedules: dict[str, str]
    pause_all: bool
    sportybet_proxy_enabled: bool

    @field_validator("schedules")
    @classmethod
    def _schedules(cls, schedules: dict[str, str]) -> dict[str, str]:
        if set(schedules) != set(SCHEDULE_NAMES):
            raise ValueError(f"schedules must be exactly {sorted(SCHEDULE_NAMES)}")
        for name, value in schedules.items():
            if not re.match(CLOCK, value):
                raise ValueError(f"{name}: {value!r} isn't a time like 21:00")
        return schedules


def default_document() -> ConfigDocument:
    return ConfigDocument.model_validate(
        {
            **load_defaults().model_dump(by_alias=True),
            "schedules": default_schedules(),
            "pause_all": False,
            "sportybet_proxy_enabled": False,
        }
    )


def document_from_stored(stored: dict[str, Any] | None) -> ConfigDocument:
    """Today's document from what DynamoDB holds. Raises if a v2 document fails validation:
    a run must not guess its thresholds (invariant 7)."""
    if not stored:
        return default_document()
    if "sports" in stored:
        return ConfigDocument.model_validate(stored)
    defaults = default_document().model_dump(by_alias=True)
    saved_times = stored.get("schedules") or {}
    defaults["schedules"] = {
        name: saved_times.get(name, value) for name, value in defaults["schedules"].items()
    }
    for key in ("dry_run", "pause_all", "sportybet_proxy_enabled"):
        if isinstance(stored.get(key), bool):
            defaults[key] = stored[key]
    return ConfigDocument.model_validate(defaults)


def load(table: Any) -> tuple[int, ConfigDocument]:
    """The version in force and its document. No saved document is version 0, the defaults."""
    item = table.get_item(Key={"pk": "CONFIG", "sk": "CURRENT"}).get("Item")
    if item is None:
        return 0, default_document()
    return int(item.get("version", 0)), document_from_stored(_plain(item.get("document")))


def _plain(value: Any) -> Any:
    """DynamoDB's Decimals as ordinary numbers, all the way down."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value
