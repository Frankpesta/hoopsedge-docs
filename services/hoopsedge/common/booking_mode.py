"""Whether a run may create real booking codes. On its own, so the API can ask cheaply.

D83 replaced D78: Run now, scheduled runs and the command line follow the same rule. Real codes
need both switches off (D97): the settings document's `dry_run` (the dashboard's switch, true by
default) and the container's `DRY_RUN` environment variable (anything but "false" keeps it dry).
Either one alone keeps every code a practice `DRYRUN-` code (invariant 8).
"""

import os


def is_dry_run(settings_dry_run: bool = True) -> bool:
    if settings_dry_run:
        return True
    return os.environ.get("DRY_RUN", "true").strip().lower() != "false"
