# Decoy database listener

A Postgres wire-protocol v3 listener that rejects every login (SQLSTATE 28P01), never serves or proxies data,
and sends one JSON event per attempt to an SQS queue through its task role.

- `trip` / `critical`: StartupMessage followed by a password attempt.
- `probe` / `low`: no valid StartupMessage within 5 s (port scan, garbage bytes).
- At most one event per source IP per 2 s; the rest are counted and logged.

## Build and push

From the repository root:

    docker buildx build --platform linux/amd64 -f captains/honeypot/listener/Dockerfile \
      -t <account>.dkr.ecr.<region>.amazonaws.com/db-backup:latest --push .

The image URI is visible in the customer's task definition. It must not contain a product word:
publish under a neutral repository name such as `db-backup`.

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `TOKEN_ID` | | honeytoken id placed in every event |
| `WORKSPACE_ID` | | workspace id placed in every event |
| `ALERT_QUEUE_URL` | required | SQS queue URL |
| `LISTEN_PORT` | `5432` | |
| `LISTEN_HOST` | `0.0.0.0` | |
| `LOG_LEVEL` | `INFO` | |

The task role needs only `sqs:SendMessage` on the queue. A private subnet without NAT needs interface endpoints
for ecr.api, ecr.dkr, logs and sqs plus an S3 gateway endpoint.
