"""Whether a run may book, on its own so the API can ask without loading the model libraries."""

import os

from hoopsedge.common.dynamo import LOCAL_ENV

LOCAL_BOOKING_ENV = "HOOPSEDGE_LOCAL_BOOKING"
DASHBOARD_TRIGGER = "dashboard"


def is_dry_run(config_dry_run: bool = False, trigger: str | None = None) -> bool:
    """D78: on the owner's machine only Run now can book, and only where the API allows it."""
    if config_dry_run:
        return True
    if os.environ.get("HOOPSEDGE_ENV", LOCAL_ENV) == LOCAL_ENV and not (
        trigger == DASHBOARD_TRIGGER and os.environ.get(LOCAL_BOOKING_ENV) == DASHBOARD_TRIGGER
    ):
        return True
    return os.environ.get("DRY_RUN", "true").strip().lower() != "false"
