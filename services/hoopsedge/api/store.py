"""The API's DynamoDB reads and writes. Queries only, never a scan (docs/09 section 8.3).

Runs and tickets are files under `data/v2/runs` (`v2.views`); DynamoDB holds the config document
and its versions, aliases, the review queue, alerts, costs, idempotency and the rate limit.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

IDEMPOTENCY_TTL = timedelta(days=1)
RESOLVED_TTL = timedelta(days=14)
CONDITION_FAILED = "ConditionalCheckFailedException"


def plain(value: Any) -> Any:
    """DynamoDB's Decimals as ordinary numbers, all the way down."""
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {key: plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [plain(item) for item in value]
    return value


def to_item(value: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = json.loads(json.dumps(value, default=str), parse_float=Decimal)
    return item


class ConflictError(Exception):
    """Someone else changed it first."""


class ApiStore:
    def __init__(self, table: Any) -> None:
        self.table = table

    def _query(self, limit: int | None = None, **query: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if limit is not None:
            query["Limit"] = limit
        while True:
            response = self.table.query(**query)
            items += response["Items"]
            if "LastEvaluatedKey" not in response or (limit and len(items) >= limit):
                return [plain(item) for item in items]
            query["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def _get(self, pk: str, sk: str) -> dict[str, Any] | None:
        item = self.table.get_item(Key={"pk": pk, "sk": sk}).get("Item")
        return None if item is None else plain(item)

    # The review queue and aliases (per sport, D89).

    def unmatched(self, sport: str) -> list[dict[str, Any]]:
        items = self._query(KeyConditionExpression=Key("pk").eq(f"UNMATCHED#{sport}"))
        return [item for item in items if item.get("status") == "open"]

    def unmatched_item(self, sport: str, event_id: str) -> dict[str, Any] | None:
        return self._get(f"UNMATCHED#{sport}", event_id)

    def owner_aliases(self, sport: str) -> dict[str, str]:
        items = self._query(KeyConditionExpression=Key("pk").eq(f"ALIAS#{sport}"))
        return {str(item["sk"]): str(item["team_id"]) for item in items}

    def delete_alias(self, sport: str, alias: str) -> bool:
        response = self.table.delete_item(
            Key={"pk": f"ALIAS#{sport}", "sk": alias}, ReturnValues="ALL_OLD"
        )
        return "Attributes" in response

    def confirm_alias(self, sport: str, alias: str, team_id: str, now: datetime) -> None:
        self.table.put_item(
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

    def close_unmatched(self, sport: str, event_id: str, status: str, now: datetime) -> None:
        self.table.update_item(
            Key={"pk": f"UNMATCHED#{sport}", "sk": event_id},
            UpdateExpression="SET #status = :status, resolved_at = :at, #ttl = :ttl",
            ExpressionAttributeNames={"#status": "status", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":status": status,
                ":at": now.isoformat(),
                ":ttl": int((now + RESOLVED_TTL).timestamp()),
            },
        )

    # Settings.

    def settings(self) -> dict[str, Any] | None:
        return self._get("CONFIG", "CURRENT")

    def config_versions(self, limit: int) -> list[dict[str, Any]]:
        """Saved versions, newest first. `CURRENT` sorts after every `v#`, so it's skipped."""
        items = self._query(
            KeyConditionExpression=Key("pk").eq("CONFIG") & Key("sk").begins_with("v#"),
            ScanIndexForward=False,
        )
        return items[:limit]

    def save_settings(
        self,
        document: dict[str, Any],
        version: int,
        changes: list[dict[str, Any]],
        now: datetime,
    ) -> None:
        """Version-checked, so two tabs can't silently overwrite each other."""
        fields = to_item(
            {
                "document": document,
                "version": version,
                "changed_by": "dashboard",
                "changed_at": now.isoformat(),
                "diff": changes,
            }
        )
        names = {f"#{key}": key for key in fields}
        values = {f":{key}": value for key, value in fields.items()}
        update = "SET " + ", ".join(f"#{key} = :{key}" for key in fields)
        condition = "attribute_not_exists(pk)" if version == 1 else "#version = :expected"
        if version > 1:
            values[":expected"] = version - 1
        try:
            self.table.update_item(
                Key={"pk": "CONFIG", "sk": "CURRENT"},
                UpdateExpression=update,
                ConditionExpression=condition,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=values,
            )
        except ClientError as error:
            if error.response["Error"]["Code"] == CONDITION_FAILED:
                raise ConflictError("Settings changed since you loaded them") from error
            raise
        self.table.put_item(Item={"pk": "CONFIG", "sk": f"v#{version:06d}", **fields})

    # Alerts and costs.

    def alerts(self, limit: int) -> list[dict[str, Any]]:
        return self._query(
            limit=limit, KeyConditionExpression=Key("pk").eq("ALERT"), ScanIndexForward=False
        )[:limit]

    def latest_cost(self) -> dict[str, Any] | None:
        items = self._query(
            limit=1, KeyConditionExpression=Key("pk").eq("COST"), ScanIndexForward=False
        )
        return items[0] if items else None

    # C16, C18: every mutation is idempotent and rate-limited, across concurrent Lambdas.

    def count_mutation(self, now: datetime) -> int:
        minute = now.astimezone(UTC).strftime("%Y%m%d%H%M")
        response = self.table.update_item(
            Key={"pk": f"RATE#{minute}", "sk": "COUNT"},
            UpdateExpression="ADD #count :one SET #ttl = :ttl",
            ExpressionAttributeNames={"#count": "count", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":one": 1,
                ":ttl": int((now + timedelta(minutes=5)).timestamp()),
            },
            ReturnValues="UPDATED_NEW",
        )
        return int(response["Attributes"]["count"])

    def claim_idempotency(self, key: str, scope: str, now: datetime) -> dict[str, Any] | None:
        """None means this request may go ahead. Otherwise it's what happened the first time."""
        try:
            self.table.put_item(
                Item={
                    "pk": f"IDEMP#{key}",
                    "sk": scope,
                    "state": "started",
                    "ttl": int((now + IDEMPOTENCY_TTL).timestamp()),
                },
                ConditionExpression="attribute_not_exists(pk)",
            )
        except ClientError as error:
            if error.response["Error"]["Code"] != CONDITION_FAILED:
                raise
            return self._get(f"IDEMP#{key}", scope) or {"state": "started"}
        return None

    def finish_idempotency(self, key: str, scope: str, status: int, body: Any) -> None:
        self.table.update_item(
            Key={"pk": f"IDEMP#{key}", "sk": scope},
            UpdateExpression="SET #state = :done, #status = :status, body = :body",
            ExpressionAttributeNames={"#state": "state", "#status": "status"},
            ExpressionAttributeValues={
                ":done": "done",
                ":status": status,
                ":body": json.dumps(body, default=str),
            },
        )

    def release_idempotency(self, key: str, scope: str) -> None:
        """A request that failed did nothing, so the same key may try again."""
        self.table.delete_item(Key={"pk": f"IDEMP#{key}", "sk": scope})


class Artifacts:
    """Files the API reads: S3 takes this role with Phase 1. Paths never come from the caller."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def read_json(self, relative: str) -> Any | None:
        path = self.root / relative
        return json.loads(path.read_text("utf-8")) if path.exists() else None

    def latest_json(self, folder: str) -> Any | None:
        files = sorted((self.root / folder).glob("*.json"))
        return json.loads(files[-1].read_text("utf-8")) if files else None

    def last_line(self, relative: str) -> dict[str, Any] | None:
        path = self.root / relative
        if not path.exists():
            return None
        lines = path.read_text("utf-8").splitlines()
        return json.loads(lines[-1]) if lines else None
