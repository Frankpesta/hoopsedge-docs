"""v2 aliases and review queue in DynamoDB (brief §5.3, D84, D89).

Same keys as v1, with the sport in place of the league: `ALIAS#NCAAF` and `UNMATCHED#NCAAF`.
Alias keys are normalized with `v2.names.normalize`. The dashboard actions come in B4; until
then `python -m hoopsedge.v2.slate alias` adds one.
"""

from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import Any

from boto3.dynamodb.conditions import Key

from hoopsedge.common.dynamo import to_item
from hoopsedge.v2.names import normalize

QUEUE_DAYS = 14  # An open queue item expires after this, like other ephemeral items.


def owner_aliases(table: Any, sport: str) -> dict[str, str]:
    response = table.query(KeyConditionExpression=Key("pk").eq(f"ALIAS#{sport}"))
    return {str(item["sk"]): str(item["team_id"]) for item in response["Items"]}


def add_alias(table: Any, sport: str, name: str, team_id: str, now: datetime) -> str:
    alias = normalize(name)
    table.put_item(
        Item={
            "pk": f"ALIAS#{sport}",
            "sk": alias,
            "gsi1pk": f"TEAM#{team_id}",
            "gsi1sk": alias,
            "team_id": team_id,
            "source": "owner",
            "confirmed_by": "owner",
            "confirmed_at": now.isoformat(),
        }
    )
    return alias


def save_unmatched(table: Any, sport: str, item: Mapping[str, Any], now: datetime) -> None:
    """One open queue item per SportyBet event; a later run replaces it with what it saw."""
    table.put_item(
        Item=to_item(
            {
                "pk": f"UNMATCHED#{sport}",
                "sk": item["event_id"],
                "home_name": item["home"]["name"],
                "away_name": item["away"]["name"],
                "start": item["start"],
                "reason": item["reason"],
                "home_team_id": item["home"]["team_id"],
                "away_team_id": item["away"]["team_id"],
                "home_suggestions": item["home"]["suggestions"],
                "away_suggestions": item["away"]["suggestions"],
                "status": "open",
                "seen_at": now.isoformat(),
                "ttl": int((now + timedelta(days=QUEUE_DAYS)).timestamp()),
            }
        )
    )
