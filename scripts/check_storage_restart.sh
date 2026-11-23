#!/usr/bin/env bash
# D84: prove the database survives a restart and a recreate of its container, and that the
# nightly export restores into an empty database.
#
#   scripts/check_storage_restart.sh            # locally
#   COMPOSE="docker compose -f docker-compose.yml -f deploy/ec2/compose.yml" \
#     scripts/check_storage_restart.sh          # on the server
#
# It writes one marker item (pk CHECK#storage) and removes it at the end. Nothing else is touched.
# The restore step uses a second, throwaway DynamoDB Local container with no volume.
set -euo pipefail
cd "$(dirname "$0")/.."
export MSYS_NO_PATHCONV=1  # Git Bash on Windows: don't rewrite the container paths below.
COMPOSE=${COMPOSE:-docker compose}
MARKER="restart-check-$(date -u +%Y%m%dT%H%M%SZ)"

mkdir -p data/dynamodb

py() {  # Runs Python in the pipeline container, against the real local database.
  $COMPOSE --profile manual run --rm -T -e MARKER="$MARKER" "$@" pipeline python -
}

step() { printf '\n== %s\n' "$1"; }

step "Start the database and write the marker"
$COMPOSE up -d --wait dynamodb
py <<'EOF'
import os
from hoopsedge.common.dynamo import connect, ensure_table, table_name
resource = connect()
ensure_table(resource, table_name())
resource.Table(table_name()).put_item(Item={"pk": "CHECK#storage", "sk": os.environ["MARKER"]})
print("written", os.environ["MARKER"])
EOF

read_marker='
import os, sys
from hoopsedge.common.dynamo import connect, table_name
item = connect().Table(table_name()).get_item(Key={"pk": "CHECK#storage", "sk": os.environ["MARKER"]}).get("Item")
print("found" if item else "MISSING", os.environ["MARKER"])
sys.exit(0 if item else 1)
'

step "Restart the container"
$COMPOSE restart dynamodb
$COMPOSE up -d --wait dynamodb
echo "$read_marker" | py

step "Remove and recreate the container"
$COMPOSE rm -sf dynamodb
$COMPOSE up -d --wait dynamodb
echo "$read_marker" | py

step "Export, then restore into an empty throwaway database"
$COMPOSE --profile manual run --rm -T pipeline python -m hoopsedge.common.backup export
latest=$(ls -1 data/backups/dynamodb/*.jsonl.gz | sort | tail -1)
echo "latest export: $latest"
$COMPOSE --profile manual run --rm -T -d --name hoopsedge-restore-check \
  --entrypoint java dynamodb -jar DynamoDBLocal.jar -inMemory -sharedDb -port 8001 >/dev/null
trap 'docker rm -f hoopsedge-restore-check >/dev/null 2>&1 || true' EXIT
sleep 4
$COMPOSE --profile manual run --rm -T -e AWS_ENDPOINT_URL_DYNAMODB=http://hoopsedge-restore-check:8001 \
  pipeline python -m hoopsedge.common.backup restore "/workspace/$latest"
echo "$read_marker" | py -e AWS_ENDPOINT_URL_DYNAMODB=http://hoopsedge-restore-check:8001

step "Remove the marker"
py <<'EOF'
import os
from hoopsedge.common.dynamo import connect, table_name
connect().Table(table_name()).delete_item(Key={"pk": "CHECK#storage", "sk": os.environ["MARKER"]})
print("removed", os.environ["MARKER"])
EOF

printf '\nOK: the database survived a restart and a recreate, and its export restores.\n'
