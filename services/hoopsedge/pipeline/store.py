"""The slate pipeline's DynamoDB items (docs/12 section 1). Nothing here is ever deleted or mutated
beyond a status: a re-run writes new versions and marks the old ones superseded (invariant 6).
"""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

from hoopsedge.engine.schemas import Leg, Prediction, Ticket
from hoopsedge.teams.matcher import Match

LOCK_MINUTES = 30
GSI = "GSI1"
PENDING_LEG = "PENDINGLEG"
PENDING_BOOKING = "pending_booking"
DRY_RUN = "dry_run"
VERIFIED = "verified"
INVALID = "invalid"
FAILED = "failed"  # Beyond docs/12: booking failed, so there's no code to call invalid (D71).
SUPERSEDED = "superseded"
CRITICAL = "CRITICAL"
WARNING = "WARNING"


def to_item(value: dict[str, Any]) -> dict[str, Any]:
    """DynamoDB takes Decimals, not floats, so go through JSON once."""
    item: dict[str, Any] = json.loads(json.dumps(value, default=str), parse_float=Decimal)
    return item


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat()


class PipelineStore:
    def __init__(self, table: Any) -> None:
        self.table = table
        self._log_number = 0

    # Lock and run.

    def acquire_lock(self, league: str, slate: date, run_id: str, now: datetime) -> bool:
        """C9: an expired lock also gives way, because TTL deletion can lag by days."""
        try:
            self.table.put_item(
                Item={
                    "pk": f"LOCK#{league}#{slate}",
                    "sk": "LOCK",
                    "run_id": run_id,
                    "ttl": int((now + timedelta(minutes=LOCK_MINUTES)).timestamp()),
                },
                ConditionExpression="attribute_not_exists(pk) OR #ttl < :now",
                ExpressionAttributeNames={"#ttl": "ttl"},
                ExpressionAttributeValues={":now": int(now.timestamp())},
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == "ConditionalCheckFailedException":
                return False
            raise
        return True

    def release_lock(self, league: str, slate: date, run_id: str) -> None:
        try:
            self.table.delete_item(
                Key={"pk": f"LOCK#{league}#{slate}", "sk": "LOCK"},
                ConditionExpression="run_id = :run",
                ExpressionAttributeValues={":run": run_id},
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise

    def save_run(self, run_id: str, fields: dict[str, Any]) -> None:
        self.table.put_item(
            Item=to_item(
                {
                    "pk": f"RUN#{run_id}",
                    "sk": "META",
                    "gsi1pk": "RUNS",
                    "gsi1sk": fields["started_at"],
                    "run_id": run_id,
                    **fields,
                }
            )
        )

    def log(self, run_id: str, now: datetime, step: str, event: str, payload: Any = None) -> None:
        self._log_number += 1
        self.table.put_item(
            Item=to_item(
                {
                    "pk": f"RUN#{run_id}",
                    "sk": f"LOG#{_stamp(now)}#{self._log_number:05d}",
                    "step": step,
                    "event": event,
                    "payload": payload,
                }
            )
        )

    def alert(self, now: datetime, severity: str, key: str, message: str, ref: str) -> None:
        self.table.put_item(
            Item={
                "pk": "ALERT",
                "sk": f"{_stamp(now)}#{key}",
                "severity": severity,
                "message": message,
                "ref": ref,
            }
        )

    # Games and teams.

    def slate_games(self, league: str, slate: date) -> list[dict[str, Any]]:
        response = self.table.query(
            IndexName=GSI, KeyConditionExpression=Key("gsi1pk").eq(f"SLATE#{league}#{slate}")
        )
        return [item for item in response["Items"] if str(item.get("pk", "")).startswith("GAME#")]

    def confirmed_aliases(self, league: str) -> list[dict[str, str]]:
        response = self.table.query(KeyConditionExpression=Key("pk").eq(f"ALIAS#{league}"))
        return [
            {"alias_normalized": str(item["sk"]), "team_id": str(item["team_id"])}
            for item in response["Items"]
        ]

    def save_unmatched(
        self,
        league: str,
        event_id: str,
        home_name: str,
        away_name: str,
        start: datetime,
        match: Match,
    ) -> None:
        def suggestions(resolved: Any) -> list[dict[str, Any]]:
            return [{"team_id": s.team_id, "score": s.score} for s in resolved.suggestions]

        self.table.put_item(
            Item=to_item(
                {
                    "pk": f"UNMATCHED#{league}",
                    "sk": event_id,
                    "home_name": home_name,
                    "away_name": away_name,
                    "start": _stamp(start),
                    "reason": match.reason,
                    "home_team_id": match.home.team_id,
                    "away_team_id": match.away.team_id,
                    "home_suggestions": suggestions(match.home),
                    "away_suggestions": suggestions(match.away),
                    "status": "open",
                }
            )
        )

    # Predictions and tickets.

    def save_prediction(self, run_id: str, prediction: Prediction) -> None:
        self.table.put_item(
            Item=to_item(
                {
                    "pk": f"GAME#{prediction.league}#{prediction.game_id}",
                    "sk": f"PRED#{run_id}",
                    **prediction.model_dump(mode="json"),
                }
            )
        )

    def next_version(self, league: str, slate: date) -> int:
        tickets = self.slate_tickets(league, slate)
        return 1 + max((int(item["version"]) for item in tickets), default=0)

    def slate_tickets(self, league: str, slate: date) -> list[dict[str, Any]]:
        response = self.table.query(
            IndexName=GSI,
            KeyConditionExpression=Key("gsi1pk").eq(f"SLATE#{league}#{slate}")
            & Key("gsi1sk").begins_with("T#"),
        )
        items: list[dict[str, Any]] = response["Items"]
        return items

    def save_ticket(
        self,
        ticket_id: str,
        ticket: Ticket,
        run_id: str,
        slate: date,
        version: int,
        status: str,
        code: str | None = None,
        reason: str | None = None,
        verification: dict[str, Any] | None = None,
    ) -> None:
        """C20: versions are zero-padded so that they sort properly."""
        self.table.put_item(
            Item=to_item(
                {
                    "pk": f"TICKET#{ticket_id}",
                    "sk": "META",
                    "gsi1pk": f"SLATE#{ticket.league}#{slate}",
                    "gsi1sk": f"T#{ticket.family}#{ticket.ticket_no:02d}#v{version:04d}",
                    "ticket_id": ticket_id,
                    "run_id": run_id,
                    "league": ticket.league,
                    "slate_date": slate.isoformat(),
                    "family": ticket.family,
                    "ticket_no": ticket.ticket_no,
                    "version": version,
                    "status": status,
                    "booking_code": code,
                    "reason": reason,
                    "verification": verification,
                    "selection_hash": ticket.selection_hash,
                    "combined_odds": ticket.combined_odds,
                    "combined_prob": ticket.combined_prob,
                    "ev": ticket.expected_value,
                    "passed_gate": ticket.passed_gate,
                    "edge_label": ticket.edge_label,
                    "settle_status": "pending",
                }
            )
        )
        # Only a finished ticket's legs: a retry can drop legs, and those must never be graded.
        if status != PENDING_BOOKING:
            for leg in ticket.legs:
                self._save_leg(ticket_id, leg, slate)

    def _save_leg(self, ticket_id: str, leg: Leg, slate: date) -> None:
        self.table.put_item(
            Item=to_item(
                {
                    "pk": f"TICKET#{ticket_id}",
                    "sk": f"LEG#{leg.rank:03d}",
                    "gsi1pk": PENDING_LEG,
                    "gsi1sk": _stamp(leg.tip_time),
                    "status": "pending",
                    "slate_date": slate.isoformat(),
                    **leg.model_dump(mode="json"),
                }
            )
        )

    # Grading (docs/08 section 7.1).

    def _all_pages(self, **query: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        while True:
            response = self.table.query(**query)
            items += response["Items"]
            if "LastEvaluatedKey" not in response:
                return items
            query["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def pending_legs(self) -> list[dict[str, Any]]:
        return self._all_pages(IndexName=GSI, KeyConditionExpression=Key("gsi1pk").eq(PENDING_LEG))

    def settled_legs(self, league: str, since: datetime) -> list[dict[str, Any]]:
        return self._all_pages(
            IndexName=GSI,
            KeyConditionExpression=Key("gsi1pk").eq(f"SETTLEDLEG#{league}")
            & Key("gsi1sk").gte(_stamp(since)),
        )

    def settled_legs_since_all(self, since: datetime) -> list[dict[str, Any]]:
        return [leg for league in ("NBA", "NCAA") for leg in self.settled_legs(league, since)]

    def settle_leg(self, leg: dict[str, Any], fields: dict[str, Any], now: datetime) -> None:
        """Moves the leg out of `PENDINGLEG` (docs/12), to where corrections can still find it."""
        names = {
            "gsi1pk": f"SETTLEDLEG#{leg['league']}",
            "settled_at": _stamp(now),
            **fields,
        }
        values = to_item({f"v{number}": value for number, value in enumerate(names.values())})
        self.table.update_item(
            Key={"pk": leg["pk"], "sk": leg["sk"]},
            UpdateExpression="SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(names))),
            ExpressionAttributeNames={f"#k{i}": name for i, name in enumerate(names)},
            ExpressionAttributeValues={f":{key}": value for key, value in values.items()},
        )

    def ticket_legs(self, ticket_id: str) -> list[dict[str, Any]]:
        return self._all_pages(
            KeyConditionExpression=Key("pk").eq(f"TICKET#{ticket_id}")
            & Key("sk").begins_with("LEG#")
        )

    def ticket_meta(self, ticket_id: str) -> dict[str, Any]:
        item: dict[str, Any] = self.table.get_item(Key={"pk": f"TICKET#{ticket_id}", "sk": "META"})[
            "Item"
        ]
        return item

    def update_ticket_settlement(
        self, ticket_id: str, status: str, odds: float, correction: bool
    ) -> None:
        update = "SET settle_status = :status, settled_combined_odds = :odds"
        values = to_item({":status": status, ":odds": odds})
        if correction:
            update += ", correction = :correction"
            values[":correction"] = True
        self.table.update_item(
            Key={"pk": f"TICKET#{ticket_id}", "sk": "META"},
            UpdateExpression=update,
            ExpressionAttributeValues=values,
        )

    def game_item(self, league: str, game_id: str) -> dict[str, Any] | None:
        item: dict[str, Any] | None = self.table.get_item(
            Key={"pk": f"GAME#{league}#{game_id}", "sk": "META"}
        ).get("Item")
        return item

    def save_aggregate(self, league: str, family: str, period: str, fields: dict[str, Any]) -> None:
        self.table.put_item(Item=to_item({"pk": f"AGG#{league}#{family}", "sk": period, **fields}))

    def daily_aggregates(self, league: str, family: str) -> list[dict[str, Any]]:
        return self._all_pages(
            KeyConditionExpression=Key("pk").eq(f"AGG#{league}#{family}")
            & Key("sk").begins_with("D#")
        )

    def supersede(self, league: str, slate: date, before_version: int) -> int:
        """Older versions of the slate stop being current, even where a re-run made fewer."""
        changed = 0
        for item in self.slate_tickets(league, slate):
            if int(item["version"]) < before_version and item["status"] != SUPERSEDED:
                self.table.update_item(
                    Key={"pk": item["pk"], "sk": "META"},
                    UpdateExpression="SET #status = :status",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={":status": SUPERSEDED},
                )
                changed += 1
        return changed

    # Idempotency (docs/07 section 6.4).

    def verified_code(self, selection_hash: str) -> str | None:
        response = self.table.get_item(Key={"pk": f"CODE#{selection_hash}", "sk": "META"})
        item = response.get("Item")
        return None if item is None else str(item["code"])

    def remember_code(self, selection_hash: str, code: str, ticket_id: str) -> None:
        self.table.put_item(
            Item={
                "pk": f"CODE#{selection_hash}",
                "sk": "META",
                "code": code,
                "ticket_id": ticket_id,
            }
        )
