# Phase 1 — Infrastructure (AWS CDK v2, TypeScript)

**Goal:** a deployable, cost-guarded skeleton: storage, one container image, schedules, a pipeline stub, an API stub and CI. Respect `CLAUDE.md` §4 at every step.

## 1.1 Stacks (`infra/lib/`)

| Stack | Contents |
|-------|----------|
| `StorageStack` | S3 bucket `hoopsedge-{env}-{account}` (block public access, SSE-S3, versioning **off**, lifecycle per §4), DynamoDB table `hoopsedge-{env}` (on-demand, PITR **off** in dev, **on** in prod: it costs little at this size and protects tickets), TTL attribute `ttl` |
| `ComputeStack` | ECR repo (lifecycle: keep 3), one `DockerImageFunction` per handler (arm64, outside a VPC), log groups with 14-day retention, reserved concurrency |
| `PipelineStack` | Step Functions state machine `slate-pipeline` (Standard), EventBridge Scheduler schedule group + schedules from `00-architecture.md` §3 |
| `ApiStack` | API Lambda + Function URL (`AuthType NONE`; a shared-secret check happens in the app, see doc 09) |
| `OpsStack` | SNS topic `hoopsedge-alerts` (email subscription to the owner, confirmed manually), AWS Budgets ($10/$25/$50 alerts to the same email), the `costs` function |

## 1.2 Lambda functions (all use the same image, different `CMD`)

| Function | Handler | Memory | Timeout | Reserved concurrency |
|----------|---------|--------|---------|----------------------|
| `ingest` | `hoopsedge.handlers.ingest.handler` | 1024 | 300 s | 2 |
| `ratings` | `...ratings.handler` | 2048 | 120 s | 1 |
| `pipeline-step` | `...pipeline.handler` (dispatches by `step` field) | 2048 | 300 s | 3 |
| `grade` | `...grade.handler` | 1024 | 300 s | 1 |
| `learn` | `...learn.handler` | 10240 | 900 s | 1 |
| `api` | `...api.handler` | 512 | 29 s | 5 |
| `costs` | `...costs.handler` | 256 | 60 s | 1 |

One `pipeline-step` function dispatching on a `step` field keeps the image count and cold starts down. The Step Functions definition passes `{"step": "Predict", ...}` etc.

Image build notes:
- Base image `public.ecr.aws/lambda/python:3.12-arm64`.
- Install with `uv pip install --system` from a lock file.
- LightGBM has arm64 wheels; verify in CI.
- Keep the image below 1 GB. Do not install PyTorch or TensorFlow.

## 1.3 IAM

- Least privilege per function: S3 prefixes it needs, the DynamoDB table, `ssm:GetParameter` on `/hoopsedge/{env}/*`, `sns:Publish` on the alerts topic.
- The `api` function additionally needs `states:StartExecution` on the pipeline, plus `scheduler:GetSchedule` and `scheduler:UpdateSchedule` on the hoopsedge schedule group.
- `costs` needs `ce:GetCostAndUsage` (Cost Explorer charges about $0.01 per request; it runs once a day).

## 1.4 Secrets / parameters (SSM Parameter Store, standard tier)

| Name | Type | Purpose |
|------|------|---------|
| `/hoopsedge/{env}/api_shared_secret` | SecureString | Vercel → API auth header |
| `/hoopsedge/{env}/sportybet_proxy_url` | SecureString | optional NG proxy |
| `/hoopsedge/{env}/cbbd_api_key` | SecureString | only if Phase 0 chose CBBD |

Cache parameters in memory per Lambda container (5 min TTL).

## 1.5 CI/CD (GitHub Actions)

- `ci.yml` on PRs:
  - ruff, mypy, pytest (unit and fixture tests, no network)
  - the web app's lint, typecheck, Vitest
  - `cdk synth`
- `deploy.yml`:
  - Manual dispatch with an `env` input.
  - Builds the arm64 image with buildx, pushes to ECR, runs `cdk deploy`.
  - Uses GitHub OIDC → an IAM role (no long-lived keys).
- The web app deploys through Vercel's GitHub integration (`web/` as root directory).

## 1.6 Cost checklist (CI enforces with a CDK assertion test)

The assertion test fails the build if `cdk synth` output contains any of the following:
- `AWS::EC2::NatGateway`
- `AWS::RDS::*`
- `AWS::ElasticLoadBalancingV2::*`
- `AWS::SecretsManager::Secret`
- `AWS::SageMaker::Endpoint`
- any Lambda with `VpcConfig`
- any Lambda not on `arm64`
- any log group without retention

## Definition of Done

- [ ] `cdk deploy -c env=dev` succeeds from CI.
- [ ] A hello-world pipeline execution runs every step stub and writes a `RUN` item.
- [ ] Budgets alerts and SNS email confirmed by the owner.
- [ ] Cost assertion test exists and passes.
- [ ] Estimated monthly cost documented (`docs/reports/cost-estimate.md`).
