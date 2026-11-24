# OpsBoard: a microservices project for AWS DevOps practice

OpsBoard is a tiny internal tool for the exact domain you want to work in: it tracks **deployments** and
**incidents**, and alerts the team by email when something important happens (a failed deploy, a sev1/sev2
incident). The app code is deliberately small. The real work is the AWS platform you build around it.

## Services

| Service | Type | Talks to | Why it exists (what it teaches) |
|---|---|---|---|
| `deploy-service` | Flask REST API | RDS (`deploydb`), SNS | Path routing on the ALB, ECS service, secrets, publishing events |
| `incident-service` | Flask REST API | RDS (`incidentdb`), SNS | A second service behind the same ALB with its own target group, IAM task role, scaling |
| `notifier` | Queue worker (no HTTP) | SQS, SNS | Async messaging, DLQ, graceful SIGTERM, an ECS service with no load balancer |
| `frontend` | Static site | `/api/*` | S3 + CloudFront with two origins |
| `cost-guard` (later) | Lambda (Python) | EC2/EBS/etc via boto3 | EventBridge schedule, least-privilege IAM, ops automation |

Each service owns its own database and shares no code with the others. That is what makes them microservices.
They talk only through HTTP (via the frontend) and events (SNS/SQS).

## Target architecture

    Browser
       |
    CloudFront
       |-- default   -> S3 (frontend, private bucket, Origin Access Control)
       |-- /api/*    -> Application Load Balancer (public subnets, 2 AZs)
                          |-- /api/deployments*  -> target group -> deploy-service   (Fargate, private subnets)
                          |-- /api/incidents*    -> target group -> incident-service (Fargate, private subnets)

    deploy-service, incident-service -> RDS PostgreSQL (isolated DB subnets): deploydb, incidentdb
    deploy-service, incident-service -> SNS topic "events" -> SQS queue (+ DLQ) -> notifier (Fargate, no ALB)
    notifier -> SNS topic "alerts" -> your email

    Platform: ECR (3 repos), Secrets Manager, CloudWatch (logs/alarms/dashboard), AWS Backup,
              CodePipeline + CodeBuild, Transit Gateway lab, CloudFormation for everything

## Repo layout

    services/deploy-service/   Flask API + Dockerfile
    services/incident-service/ Flask API + Dockerfile
    services/notifier/         SQS worker + Dockerfile
    frontend/                  Static dashboard
    gateway/nginx.conf         Local stand-in for CloudFront + ALB routing
    db/init.sql                Creates the per-service databases locally
    cloudformation/            One stack per layer (we write these together)
    lambda/                    cost-guard (later)
    docker-compose.yml         Local dev

## Stages

Each stage ends with something working you can demo.

| # | Stage | What you build | JD skills covered |
|---|-------|----------------|-------------------|
| 0 | Account setup | Root MFA, admin user or IAM Identity Center, Budget alert, AWS CLI | IAM, cost control |
| 1 | Local run | `docker compose up`, hit every endpoint | Docker, health checks |
| 2 | `01-network.yaml` | VPC, public/private/db subnets in 2 AZs, IGW, NAT, route tables, security groups | VPC, Security Groups, Route Tables |
| 3 | `02-data.yaml` | RDS PostgreSQL, subnet group, Secrets Manager, AWS Backup plan, create the two DBs with a one-off ECS task, one tested restore | RDS, AWS Backup |
| 4 | `03-messaging.yaml` | SNS events topic, SQS queue + DLQ, alerts topic with email subscription | Event-driven design |
| 5 | `04-platform.yaml` + `05-services.yaml` | 3 ECR repos, ECS cluster, ALB with path-based rules, 3 task definitions, per-service task roles, autoscaling, CloudWatch logs | ECS, ALB, IAM |
| 6 | `06-frontend.yaml` | S3 bucket, CloudFront with S3 origin and ALB origin, OAC, invalidation | S3, CloudFront |
| 7 | `07-pipeline.yaml` | CodePipeline + CodeBuild: build 3 images, push to ECR, deploy to ECS | CI/CD |
| 8 | `08-monitoring.yaml` | Alarms (ALB 5xx, task CPU, **DLQ depth > 0**, queue age), dashboard, Logs Insights queries, an outage drill | CloudWatch |
| 9 | `09-tgw-lab.yaml` | Second "tools" VPC attached via Transit Gateway; reach RDS from it | Transit Gateway |
| 10 | `lambda/cost_guard.py` | Scheduled Lambda that reports untagged and idle resources | Python/boto3 automation |
| 11 | Docs | `RUNBOOK.md` (symptom -> cause -> fix), architecture diagram | Documentation |

Optional: Jenkins on EC2 as a second pipeline (the JD mentions it), blue/green deploys with CodeDeploy, WAF on CloudFront.

## Run locally (Stage 1)

    docker compose up --build
    open http://localhost:8080

    curl -X POST localhost:8080/api/deployments -H 'Content-Type: application/json' \
      -d '{"service":"checkout-api","version":"v1.0.0","environment":"prod","status":"failed"}'
    curl -X POST localhost:8080/api/incidents -H 'Content-Type: application/json' \
      -d '{"title":"Checkout 500s","service":"checkout-api","severity":"sev1"}'
    docker compose logs deploy-service    # see the structured JSON logs and the local-only event

Locally, events are only logged (no SNS yet). The log line `event_local_only` shows exactly what
will flow through SNS and SQS once we reach Stage 4.

## Working rules

1. Nothing is created by clicking. Explore in the console, then express it in CloudFormation.
2. One stack per layer. Stacks share values through `Outputs` and `Fn::ImportValue`.
3. Tag everything with `Project=opsboard` and `Env=dev`.
4. Tear down at the end of every session, in reverse order. Empty the S3 bucket and ECR repos first.
5. Every problem you hit goes into `RUNBOOK.md` as symptom -> cause -> fix.
6. Each service gets its own IAM task role with only the permissions it needs.

## Cost guardrails

- Set a Budget alert before creating anything.
- NAT Gateway, ALB, Fargate tasks, RDS and Transit Gateway attachments bill by the hour even when idle.
  Create them when a session starts and delete the stacks when it ends.
- Check which services your account plan allows before Stage 5 and Stage 9.

## Resume bullets (add real numbers when you have them)

- Designed and deployed a 3-service microservices platform on AWS (ECS Fargate, ALB, RDS PostgreSQL, SNS/SQS) across 2 Availability Zones, defined entirely in CloudFormation.
- Implemented path-based ALB routing, per-service IAM task roles, Secrets Manager credentials, and private/isolated subnet tiers.
- Built event-driven alerting with SNS fan-out, an SQS queue with a dead-letter queue, and a worker with graceful shutdown.
- Created a CodePipeline/CodeBuild CI/CD pipeline that builds images, pushes to ECR, and deploys to ECS on each commit.
- Set up CloudWatch logs, alarms (including DLQ depth), and dashboards, and AWS Backup with tested restores.
- Connected a separate tools VPC through Transit Gateway; wrote a Python Lambda to report untagged and idle resources.
- Authored an operational runbook from real failures and recoveries.
