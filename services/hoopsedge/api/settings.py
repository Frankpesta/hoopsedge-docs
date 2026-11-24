"""The settings document, cut down for v2 until B4 replaces it with the v2 config document (D96).

What's left is what the v2 jobs still read: schedule times (the scheduler's job names), the dry-run
switch, `pause_all` and the proxy flag. The v1 market thresholds went to `legacy/` with the model
that produced them; v2 thresholds live in `common/config_v2`. A settings document saved before v2
still loads: only its v2 fields are kept, and missing ones take their defaults.
"""

from typing import Any

from hoopsedge.pipeline.schedule import BACKUP_TIME, SCORES_TIME, SLATES, SPORTS

FAMILIES = (
    "handicap",
    "total_ft",
    "total_1h",
    "team_total",
)  # v1 ticket families, for old tickets.


def default_schedules() -> dict[str, str]:
    times = {"backup": BACKUP_TIME, **{f"{s.lower()}_scores": SCORES_TIME for s in SPORTS}}
    times |= {name: at for name, (_, at, _) in SLATES.items()}
    return {name: at.strftime("%H:%M") for name, at in times.items()}


SCHEDULE_NAMES = tuple(default_schedules())


def default_settings() -> dict[str, Any]:
    return {
        "schedules": default_schedules(),
        "sportybet_proxy_enabled": False,
        # C12: true until someone turns it off on purpose. The environment can still force it on.
        "dry_run": True,
        "pause_all": False,
    }


def v2_document(stored: dict[str, Any]) -> dict[str, Any]:
    """A saved document in today's shape: v2 fields kept, v1-only ones dropped."""
    defaults = default_settings()
    document = {key: stored[key] for key in defaults if key in stored}
    saved_times = stored.get("schedules") or {}
    document["schedules"] = {
        name: saved_times.get(name, default) for name, default in defaults["schedules"].items()
    }
    return {**defaults, **document}


def changes_between(old: Any, new: Any, path: str = "") -> list[dict[str, Any]]:
    """Every value that differs, so the audit and the diff preview say exactly what changed."""
    if isinstance(old, dict) and isinstance(new, dict):
        found: list[dict[str, Any]] = []
        for key in sorted(old.keys() | new.keys()):
            found += changes_between(old.get(key), new.get(key), f"{path}.{key}" if path else key)
        return found
    return [] if old == new else [{"path": path, "from": old, "to": new}]
