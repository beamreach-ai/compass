# Honeytokens on AWS

A honeytoken is a credential or object with no legitimate use: a decoy AWS
access key, a database password, an S3 object called `secrets/…env`. Nobody
who belongs there ever touches it, so the first use is an alarm with almost no
false positives. For the idea itself, where decoys work and where they do not,
and how to build them by hand, read the vendor-neutral guide
[AWS canary credentials](https://beamreach.ai/docs/aws-canary-credentials.html).
This page is how Compass does it.

Compass plants decoys as Terraform in your own repository, receives their
alerts on a queue it owns, and turns each trip into an incident with
the source, the blast radius and, if you allow it, a response. Nothing is
created in your account until you merge and apply the pull request.

## What you need

- Compass running ([quickstart](quickstart.md)) and a first map scan done:
  suggestions and investigation read the map.
- `compass connect` run once ([quickstart](quickstart.md),
  [access.md](access.md)). Besides the roles Compass reads and acts through,
  it creates the alert queue, one per region in `regions=`.
- A Terraform root module in a GitHub repository that you apply: the
  `github_repo` and `terraform_root` you gave `init_config`.
- For `aws_credential` decoys, a CloudTrail trail that logs management events
  in the account. `honeypot arm` refuses to arm a key without one; Compass
  never creates the trail for you.

## Planting

```sh
docker compose run --rm compass honeypot create honeytoken_class=aws_credential name=svc_nightly_backup region=us-east-1
```

The alert queue lives in the account Compass runs from (the same account
is fine), with SQS-managed encryption. Its policy lets the target account's
own roles and its EventBridge rules send, and only Compass's runtime identity
receive.

`create` records the decoy and starts a plant job, which opens a pull request
on a `beamreach/honeypot/<name>` branch with:

- `services/<name>/`: the decoy's module, one per decoy.
- `services/_account/`: once per account, the role EventBridge uses to deliver
  to the queue.
- `services.tf` at the root: the module blocks that wire the two together.

Everything AWS will show is derived from the decoy's cover name
(`svc_nightly_backup` above): resource names, rule names, object keys. No
product words appear, and a name that contains one is replaced with a cover
name. Each taggable resource carries one tag, `cost-center = <8 hex>`, which
is opaque; the link to the decoy stays in Compass's tables.

Merge and apply the pull request yourself. Then:

```sh
docker compose run --rm compass honeypot arm honeytoken_id=<id>      # secret_access_key=… keeps the secret with the decoy
docker compose run --rm compass honeypot fire honeytoken_id=<id> secret_access_key=<terraform output -raw …>
docker compose run --rm compass honeypot incidents
```

`arm` checks, through the read role, that the resources are in place and
marks the decoy armed. `fire` makes one STS call with an `aws_credential` decoy's key and
waits for the alert to come back through the queue, which proves the whole
path. Secrets (the access key's secret, the SSH private key) are sensitive
module outputs and are never re-exported at the root. Compass records the
access key id when it arms the decoy, and the secret only if you pass it to
`arm`.

The dashboard's Decoys tab does the same, and suggests placements from the
map: for example a decoy object for an S3 bucket, or a decoy key next to an
IAM role or a secret. The **autopilot** setting plants every suggestion with
default settings; each plant is still a pull request. Lambda functions, load
balancers, DynamoDB tables and ECR repositories are suggested `api_token`
decoys, which do not report yet (below), so review the list before you turn
autopilot on.

## Classes, and what trips them

| Class | What the pull request creates | What trips it |
|---|---|---|
| `aws_credential` | An IAM user under `/service/` with an inline policy that denies every action, and one access key. | Any API call made with the key that CloudTrail logs, read-only ones included (`sts:GetCallerIdentity`): the rule receives all management events. |
| `s3_object` | An object in a bucket you name (default key `secrets/<cover>.env`), and a CloudTrail trail that logs data events for that one object (`enable_data_trail = false` if a trail already does). | `GetObject` or `HeadObject` on the object. |
| `ssh_key` | An EC2 key pair (the private key is a sensitive output) and a security group. | An EC2 API call that names the key pair, such as launching an instance with it. Logging in over SSH is not a CloudTrail event and does not trip it. |
| `db_credential` | A Fargate service running the decoy listener, a Postgres endpoint that rejects every login, behind a Cloud Map name. | A login attempt (critical) or a connection that never logs in (a probe, low). |
| `api_token` | A token and a Lambda function URL that answers 401. | Not reported to Compass in this release: the function's callback is not wired to anything. |
| `dns_record` | An `A` record `<cover>-internal`, query logging, a metric filter, an alarm and an SNS topic. | Not reported to Compass in this release: the alarm's message does not reach the queue as a decoy alert. |

Rely on the first four. `db_credential` needs two settings in the workspace
config, `honeypot.network` (the VPC and private subnets the listener runs in)
and `honeypot.listener_image`, an image you build from
`captains/honeypot/listener/` and push to your own registry under a neutral
name ([its README](../captains/honeypot/listener/README.md)).

EventBridge rules are regional: a decoy sees the calls made in the region its
module is applied in, and EventBridge cannot deliver to a queue in another
region. Decoys in several regions need a queue in each:
`compass connect regions=us-east-1,eu-west-1` creates them, and each plant
points at the queue for its region.

## Where to put them

A decoy is only as good as its placement: somewhere an intruder or a leak
would carry it, and nowhere your own systems would use it.

- **Leaked source code or config.** Put an `aws_credential` key in a
  `.env` or `~/.aws/credentials` file in a repository, a CI variable, a wiki
  page, a container image. The decoy's material includes ready snippets for
  both file formats. Any use of the key, from anywhere, trips it.
- **Inside a running service.** With placement `ecs_task_env`, the plant pull
  request also adds the key to a container's `environment` in your own
  `aws_ecs_task_definition`, wired to the module's outputs so no secret is
  written into the repository. An intruder in that container who reads its
  environment and uses the key is caught, and the incident names the task.
  Compass edits only a literal `jsonencode([...])` container definition and
  refuses anything else with instructions.
- **Data stores.** An `s3_object` in a bucket worth reading; a `db_credential`
  next to a real database, with its password left where a connection string
  would be.

## When one trips

The daemon (`compass serve`) long-polls the alert queue. For each message it:

1. Records the alert: the API call, region, source IP (classified as
   internal, external or an AWS service), user agent and the principal.
2. Groups it into an incident: alerts from the same placement and the same
   source task or IP join one open incident; a trip after containment opens a
   new one, linked to the first.
3. Notifies the endpoints you added: Slack, a webhook or email
   (`honeypot endpoint_add`, or Settings).
4. Investigates: from a private source IP to the ECS task that made the call
   (through its network interface), to the task's IAM role, to what that role
   can reach in the map. That is the blast radius.
5. Responds, if you enabled a response, and posts a summary when a model is
   configured.

An alert that cannot be read stays on the queue rather than being dropped.

## Responses

Two actions exist. Neither can run until you enable it and merge the pull
request that grants it:

| Action | What it does |
|---|---|
| `iam_deactivate_key` | Deactivates the access key that was used. |
| `ecs_restart_task` | Stops the ECS task that used the key; its service replaces it from the image. |

```sh
docker compose run --rm compass honeypot action_set action_type=iam_deactivate_key mode=autopilot risk_max=2
```

Each enabled action has a scope (`service_arns`, `user_arns`,
`user_path_prefixes`), a mode and a `risk_max`:

- `off`: recommend only.
- `approve`: wait for a person to approve it on the incident.
- `autopilot`: run it when its risk score is at or below `risk_max`,
  otherwise wait for a person.

The risk score comes from fixed rules over the action's type and the
resource it touches; the model never sets it. Without a model, every
enabled action whose scope covers the incident goes through that gate. With
`HONEYPOT_AGENT_CHOICE=llm` the model chooses among those actions, and its
answer is checked against the same list. Each run writes an audit record
before its first AWS call, takes a snapshot, verifies the result, and keeps
a rollback you can run from the audit trail where the action supports one.

Enabling an action is a *grant*. It opens a pull request on your
infrastructure repository that gives Compass's act role exactly that
permission, and nothing else. The action is armed only after the pull
request is merged and applied and Compass has read the role back from IAM
and simulated the permission; until then it waits, whatever its mode says.
Removing the action disarms it at once and opens the pull request that takes
the permission away. Each run uses its own session, narrowed to that grant
and named for the person who approved it, so the account's CloudTrail shows
who allowed it. `compass access list` and Settings → Account → Access show
every grant and its state; [access.md](access.md) has the whole model. Grant
the actions before you need them: nobody merges an IAM pull request
mid-incident.

## Removing a decoy

`honeypot decommission` opens a pull request that removes the decoy's module
block and directory (and, for `ecs_task_env`, its variables from the task
definition). The account module stays for the other decoys.
