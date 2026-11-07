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
- **The laptop must be on, awake and online** for the slate runs (15:00, 22:00 and 00:30 Lagos),
  for Run now and for odds snapshots. Without it, SportyBet steps fail and the run alerts.
  Everything else keeps running.
- **A paid proxy instead:** set `HOOPSEDGE_SPORTYBET_PROXY=http://user:pass@host:port` and
  recreate `api scheduler collector`. The laptop relay can then be stopped.

## Start, update and stop

```sh
dc up -d aws-mock collector live-ingest          # data first
dc run --rm pipeline python -m hoopsedge.learning.run bootstrap --league NBA
dc run --rm pipeline python -m hoopsedge.learning.run bootstrap --league NCAA
dc up -d --no-deps scheduler api caddy
dc ps
```

Bootstrap returns the existing champion when there is one: champions are files in
`data/models`, so they survive restarts. It is only needed on a fresh `data/` folder.

To deploy a new commit from `main`:

```sh
git pull --ff-only
sed -i "s/^HOOPSEDGE_GIT_SHA=.*/HOOPSEDGE_GIT_SHA=$(git rev-parse --short HEAD)/" .env
dc build scheduler api collector live-ingest pipeline
dc up -d --no-deps scheduler api collector live-ingest
```

Always use `--no-deps`: recreating `aws-mock` wipes tickets, runs, aliases and Settings.

The server's tree is a clone of `main`. Files copied in by hand before a commit is merged make
`git pull` refuse: once they're on `main`, `git checkout -- . && git clean -fd deploy services`
then pull.

Pause the scheduler with `touch data/scheduler/PAUSED`. Stop everything with `dc stop`.

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
- `aws-mock` keeps everything in memory. A restart of it, or of the server, loses tickets, runs,
  confirmed aliases and Settings (which fall back to defaults, dry run on). Files in
  `data/` survive. Moving to persistent storage is a follow-up.
- Real codes come only from the dashboard's Run now (D78). Scheduled and command-line runs stay
  practice.
- The dashboard has no login (D8): anyone with the Vercel address can use it.
