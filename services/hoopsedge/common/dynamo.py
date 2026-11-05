"""The DynamoDB single table (docs/12-data-model.md section 1). GAME# items only, so far."""

import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
LOCAL_ENV = "local"


def table_name() -> str:
    return f"hoopsedge-{os.environ.get('HOOPSEDGE_ENV', LOCAL_ENV)}"


def connect() -> Any:
    """Return a DynamoDB resource. Locally, refuse anything but an explicit stand-in endpoint."""
    endpoint = os.environ.get("AWS_ENDPOINT_URL_DYNAMODB")
    if os.environ.get("HOOPSEDGE_ENV", LOCAL_ENV) == LOCAL_ENV and not endpoint:
        raise RuntimeError("Local runs need AWS_ENDPOINT_URL_DYNAMODB, so AWS is never reached")
    return boto3.resource("dynamodb", region_name=REGION, endpoint_url=endpoint)


def ensure_table(resource: Any, name: str) -> None:
    """Create the table on the local stand-in, which forgets everything when it restarts."""
    try:
        resource.create_table(
            TableName=name,
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": key, "AttributeType": "S"}
                for key in ("pk", "sk", "gsi1pk", "gsi1sk")
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "GSI1",
                    "KeySchema": [
                        {"AttributeName": "gsi1pk", "KeyType": "HASH"},
                        {"AttributeName": "gsi1sk", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
        )
    except ClientError as error:
        if error.response["Error"]["Code"] != "ResourceInUseException":
            raise


class GameTable:
    def __init__(self, table: Any) -> None:
        self.table = table

    def get_game(self, league: str, game_id: str) -> dict[str, Any] | None:
        response = self.table.get_item(Key={"pk": f"GAME#{league}#{game_id}", "sk": "META"})
        item: dict[str, Any] | None = response.get("Item")
        return item

    def upsert_game(self, item: dict[str, Any]) -> bool:
        """Write only when something changed, because each write costs money in production."""
        stored = self.get_game(item["league"], item["game_id"])
        if stored is not None and all(stored.get(key) == value for key, value in item.items()):
            return False
        self.table.put_item(Item=item)
        return True
