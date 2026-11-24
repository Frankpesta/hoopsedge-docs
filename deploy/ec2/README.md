# HoopsEdge on EC2 (D80)

The whole Docker Compose stack runs on one always-on EC2 instance. The dashboard runs on Vercel and
calls the API over HTTPS through Caddy. This replaces docs/02's serverless plan for now, on the
owner's written approval (2026-11-07).

| | |
|---|---|
| Instance | `m7i-flex.large` (2 vCPU, 8 GB), 40 GB disk, Amazon Linux 2023, x86_64 |
| Region | `eu-north-1` (Stockholm) |
| Address | Elastic IP `13.48.245.26`, login `ec2-user`, key `edge.pem` |
| API | `https://13-48-245-26.sslip.io`, through Caddy; every request needs the shared secret |
| Open ports | 22 (owner's IP), 80 and 443 (anywhere, for the certificate and Vercel) |

`sslip.io` turns the IP into a hostname, so Caddy can get a free certificate without a domain.
If the Elastic IP ever changes, change `HOOPSEDGE_API_HOST` and Vercel's `HOOPSEDGE_API_URL`.

## Connecting

```sh
ssh -i ~/.ssh/hoopsedge-ec2.pem ec2-user@13.48.245.26
cd ~/hoopsedge
```

Every command below runs there, in `~/hoopsedge`. `dc` stands for
`docker compose -f docker-compose.yml -f deploy/ec2/compose.yml --profile manual`:

```sh
alias dc='docker compose -f docker-compose.yml -f deploy/ec2/compose.yml --profile manual'
```

## The server's `.env`

Never committed. `chmod 600`.

```
HOOPSEDGE_API_KEY=<the same long random value as Vercel's>
HOOPSEDGE_GIT_SHA=<short commit the images were built from>
HOOPSEDGE_API_HOST=13-48-245-26.sslip.io
HOOPSEDGE_SPORTYBET_PROXY=socks5h://host.docker.internal:1080
```

## SportyBet through the laptop (D81)

SportyBet's CloudFront WAF challenges the app from AWS addresses (status 202,
`x-amzn-waf-action: challenge`). So SportyBet's requests, and only those, leave from the owner's
home connection through an SSH tunnel the laptop opens:

```
EC2 containers -> socks5h://host.docker.internal:1080 -> SSH tunnel -> laptop -> www.sportybet.com
```

- **Laptop:** `docker compose -f deploy/relay/compose.yml up -d --build`, once. It starts with
  Docker Desktop and reconnects by itself. It can only reach `www.sportybet.com:443`.
- **Server:** `deploy/ec2/sshd-relay.conf` is installed as
  `/etc/ssh/sshd_config.d/60-hoopsedge-relay.conf`, so the tunnel listens on the Docker bridge
  address `172.17.0.1:1080` and nowhere else. Check it with `sudo ss -ltn | grep 1080`.
- **The laptop must be on, awake and online** for the slate runs (NCAAB and NBA 16:00 and
  21:00, NCAAF 18:00 and Saturdays 14:00 Lagos) and for Run now. Without it, SportyBet steps fail
  and the run is marked failed. Everything else keeps running.
- **A paid proxy instead:** set `HOOPSEDGE_SPORTYBET_PROXY=http://user:pass@host:port` and
  recreate `api scheduler`. The laptop relay can then be stopped.

## Start, update and stop

```sh
mkdir -p data/dynamodb                           # once, as ec2-user (the database runs as uid 1000)
dc up -d dynamodb
for sport in NCAAF NCAAB NBA; do dc run --rm pipeline python -m hoopsedge.ingest.scores refresh --sport $sport; done
dc up -d --no-deps scheduler api caddy
dc ps
```

The first `refresh` of each sport backfills its current season; later ones only fetch recent days.
Since D96 the v1 `collector` and `live-ingest` services and the model bootstrap are gone: after
pulling that change, remove their containers with `dc rm -sf collector live-ingest`.

To deploy a new commit from `main`:

```sh
git pull --ff-only
sed -i "s/^HOOPSEDGE_GIT_SHA=.*/HOOPSEDGE_GIT_SHA=$(git rev-parse --short HEAD)/" .env
dc build scheduler api pipeline
dc up -d --no-deps scheduler api
```

Use `--no-deps`, so the database isn't restarted for nothing. Since D84 a restart or recreate of
`dynamodb` loses nothing: its file is `data/dynamodb/shared-local-instance.db`.

The server's tree is a clone of `main`. Files copied in by hand before a commit is merged make
`git pull` refuse: once they're on `main`, `git checkout -- . && git clean -fd deploy services`
then pull.

Pause the scheduler with `touch data/scheduler/PAUSED`. Stop everything with `dc stop`.

## The database (D84, D91)

DynamoDB Local keeps its file in `data/dynamodb`. The scheduler exports the whole table every day
at 05:00 Lagos into `data/backups/dynamodb/` and keeps the newest 14. By hand:

```sh
dc run --rm pipeline python -m hoopsedge.common.backup export
scripts/check_storage_restart.sh                     # with COMPOSE set to the dc command, see the script
```

To restore, stop the services that write, empty the database, then load an export:

```sh
dc stop scheduler api
dc rm -sf dynamodb && mv data/dynamodb data/dynamodb.broken && mkdir data/dynamodb
dc up -d dynamodb
dc run --rm pipeline python -m hoopsedge.common.backup restore /workspace/data/backups/dynamodb/<file>.jsonl.gz
dc up -d --no-deps scheduler api
```

**Moving from `aws-mock` (once, when this lands on `main`).** `aws-mock` held everything in memory,
so keep what it has before removing it. The still-running `aws-mock` is exported with the new code:

```sh
git pull --ff-only
sed -i "s/^HOOPSEDGE_GIT_SHA=.*/HOOPSEDGE_GIT_SHA=$(git rev-parse --short HEAD)/" .env
mkdir -p data/dynamodb
dc build scheduler api pipeline
dc run --rm --no-deps -e AWS_ENDPOINT_URL_DYNAMODB=http://aws-mock:5000 \
  -e HOOPSEDGE_DATA_DIR=/workspace/data/aws-mock-final pipeline python -m hoopsedge.common.backup export
ls data/aws-mock-final/backups/dynamodb/                 # check the export is there first
dc up -d dynamodb && dc up -d --no-deps scheduler api
docker rm -f hoopsedge-aws-mock-1
```

The old state is not loaded into the new database. v2 renames NCAA to NCAAB (D87), and nothing in
it has to carry over; the file stays for reference.

## Checking on it

```sh
dc ps
dc logs --tail 50 scheduler
tail -5 data/scheduler/runs.jsonl
curl -s https://13-48-245-26.sslip.io/health
free -h; df -h /
```

## Known limits

- SportyBet depends on the laptop relay (above).
- Backups (D91) stay on the server: `data/backups/dynamodb/`, the newest 14 nightly exports.
  Losing the instance's disk loses them too.
- Real codes come only from the dashboard's Run now (D78). Scheduled and command-line runs stay
  practice.
- The dashboard has no login (D8): anyone with the Vercel address can use it.
