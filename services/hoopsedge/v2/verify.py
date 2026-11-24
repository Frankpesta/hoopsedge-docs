"""Read a booked code back and compare it with what was meant (brief §2.4, invariant 4, audit 8).

Pure: the caller fetches the share lookup. A code is verified only if the lookup holds exactly as
many selections as intended and every selection matches on event, market, specifier and outcome.
Anything else, including a lookup we can't read, is a failed code.
"""

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from hoopsedge.sportybet.discovery import SelectionKey


@dataclass(frozen=True)
class CodeCheck:
    verified: bool
    reason: str | None
    missing: tuple[SelectionKey, ...] = ()
    extra: tuple[SelectionKey, ...] = ()


def _key(selection: dict[str, str]) -> SelectionKey:
    return (
        selection["eventId"],
        selection["marketId"],
        selection.get("specifier") or None,
        selection["outcomeId"],
    )


def _decoded(document: dict[str, Any]) -> Counter[SelectionKey]:
    return Counter(
        (
            str(event["eventId"]),
            str(market["id"]),
            market.get("specifier") or None,
            str(outcome["id"]),
        )
        for event in document["data"]["outcomes"]
        for market in event["markets"]
        for outcome in market["outcomes"]
    )


def check_code(selections: Sequence[dict[str, str]], document: dict[str, Any]) -> CodeCheck:
    try:
        found = _decoded(document)
    except (KeyError, TypeError, AttributeError) as error:
        return CodeCheck(False, f"unreadable lookup: {type(error).__name__}: {error}")
    wanted = Counter(_key(selection) for selection in selections)
    missing = tuple(sorted((wanted - found).elements(), key=str))
    extra = tuple(sorted((found - wanted).elements(), key=str))
    if sum(found.values()) != sum(wanted.values()):
        reason = f"{sum(found.values())} legs in the code, {sum(wanted.values())} intended"
        return CodeCheck(False, reason, missing, extra)
    if missing or extra:
        return CodeCheck(False, f"{len(missing)} selections differ", missing, extra)
    return CodeCheck(True, None)
