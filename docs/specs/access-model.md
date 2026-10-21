# The access model: a read-only Compass, writes granted by pull request, one session for every call

Status: draft 2026-10-19, revised 2026-10-20 with nine decisions (D1–D9,
"Decided" near the end), for review. Roadmap items 3.1–3.12 (angle 3).
Line references are to `main` at b34f14c.

## 1. Goals and non-goals

**Goals.**

- Compass only reads. Its write access in a target account is exactly the set
  of actions a person has whitelisted, and every one of those is granted and
  revoked by a pull request that a person merges and applies.
- The operator's AWS profile is used once, by `compass connect`, to create the
  roles and the alert forwarding. Compass never uses it to reach a target
  account, and the running containers never see it.
- One broker builds every AWS session, first-party captains included. Each
  session is named for the captain and the job that asked for it, carries the
  person who approved it, and is narrowed to what that captain declared. The
  target account's CloudTrail then answers "which captain, which job, approved
  by whom" without Compass's own logs.
- Decoy alerts flow *out* of the target account into the account Compass
  belongs to. After `connect`, nothing Compass does writes into a target
  account except through a whitelisted, granted action.
- A captain's manifest declares what it reads and what it may propose to do.
  `compass validate` checks it, the Captains page shows it before install, and
  the broker enforces it.
- Same-account installs keep working. Control and target may be one AWS
  account; the separation is by identity (roles), not by account.
- The interface is cloud-neutral. Azure is the likely second provider; AWS
  specifics live in one provider module, not in the broker's signature or the
  manifest's shape.

**Non-goals (for now; the design leaves room, nothing more).**

- 3.7 out-of-process third-party captains: the broker is the one thing a
  worker container would need; section 10 says what a worker gets and nothing
  is built.
- 3.9 one policy engine: `policy.gate` stays as it is; the grant state is
  checked beside it, not inside it.
- 3.10 tamper-evident audit and secrets: the audit rows written today keep
  their shape; the credentials store (6.6) is named where secrets would move.
- Azure as a target: the manifest's `permissions` is keyed by provider so an
  `azure:` block can appear later; nothing reads one.
- A UI redesign. Section 12 says what Settings → Account shows; the rest of
  the dashboard does not change.
- Multi-account (1.8): one target per workspace, as today. `connect` is
  written so running it against a second account adds a second `aws.targets[]`
  entry one day; nothing reads more than one.

## 2. Current state

What the code does today, from reading it.

**The profile reaches the target directly.** `init_config` writes
`aws: {auth_mode: local_iam}` (`compass_core/commands.py:86`); the image sets
`COMPASS_ALLOW_LOCAL_IAM=1` (`docker/Dockerfile:19`); compose mounts
`${AWS_DIR:-~/.aws}` read-only into both Compass services and sets
`AWS_PROFILE` (`docker-compose.yml`, the `x-compass` anchor). With those three,
`build_target_session` (`compass_core/aws.py:135-181`) returns the ambient
session (`:179-180`) for every capability, so the mapper, the decoys, the
responder and terraform all act as the operator's profile. The quickstart says
so in its first sentence ("your credentials, your account").

**The pieces of a role-based model exist and are not the default.**
`build_target_session` can also use static keys or `AssumeRole` per capability
(`read` / `write` / `respond`), with an external id (`aws.py:90-112`,
`:152-177`). `respond` is explicit and never falls back to the read or write
role (`:96-100`). The session name is
`beamreach-target-<capability>-<epoch>` (`:162`): it says nothing about which
captain or job made the call, and CloudTrail cannot tell them apart. There is
no `SourceIdentity` and no session policy. The API's AWS settings
(`compass_api/routes/settings.py:215-260`) validate `local_iam`, `assume_role`
or `access_key`; the default role name is `BeamreachCompassRole` (`:75`); the
external id is generated as `beamreach-compass-<token>` (`:172-190`). No
policy document for a read role is shipped anywhere; the role is the user's
problem.

**Where target sessions are built (12 sites).** The mapper
(`captains/mapper/captain.py:67`, `read`); the decoy captain's placement check
(`captains/honeypot/captain.py:325`, `read`) and `arm`
(`captains/honeypot/commands.py:208`, `read`;
`compass_api/routes/honeypots.py:934`, `read`); the responder
(`captains/honeypot/respond.py:211`, `respond`; `compass_api/routes/honeypot_actions.py:146`
for `/check` and `:302` for a rollback, both `respond`, both **inside the API
request**); the IaC import, which freezes read credentials into terraform's
environment (`captains/iac/import_job.py:105` → `aws.py:184-221`); the
settings routes (`settings.py:140` resolves the local account, `:241`
validates); `init_alerts` (`commands.py:301`, `write`); and `init_config`,
which resolves the account id with a read session at setup time
(`commands.py:147`).
The mapper also makes a *control* session from the ambient credentials
(`captains/mapper/captain.py:80`) and re-derives regional sessions from the
target's frozen credentials (`captains/mapper/aws_mapper.py:1417-1425`); the
`arm` route re-pins its session to the placement's region the same way
(`compass_api/routes/honeypots.py:939`).

**Where ambient AWS credentials are used for Compass's own needs (23 sites).**
DynamoDB (`compass_core/tables.py:79`, `jobs.py:94-96`, `registry.py:137`,
`commands.py:232`, `infra_map/store.py:48`, `fact_store.py:32`,
`captains/prs/notify.py:59`, `captains/prs/scheduler.py:35`, `captains/honeypot/captain.py:707,734`,
`captains/honeypot/alert_consumer.py:126`, `captains/honeypot/alert_dispatcher.py:73,393`,
`compass_api/auth/tokens.py:106`,
`routes/honeypots.py:1035`); Secrets Manager (`compass_core/secrets.py:39`,
`auth/tokens.py:339`, `routes/slack_connect.py:76`); SES
(`alert_dispatcher.py:593`, `routes/notifications.py:442,489`); STS for the
control account id (`routes/settings.py:69`); and **SQS: the alert consumer
polls the decoy queue with the ambient identity** (`alert_consumer.py:81`).
That queue is created *in the target account* by `init_alerts`
(`commands.py:297-309`, `capability="write"`), with a policy that lets the
account's EventBridge rules and the account's own principals send
(`:272-295`). Today this works only because control and target are the same
profile.

**Decoy alerts already travel through a role.** Every rule-based decoy module
creates an `alert_delivery` IAM role with `sqs:SendMessage` on
`var.alert_queue_arn` and points its EventBridge target at the queue through
that role (`captains/honeypot/terraform_generator.py:125-145`, `:201-204`).
The `db_credential` listener task sends with its task role
(`captains/honeypot/listener/server.py:33-39`). Cross-account delivery to a
queue elsewhere is therefore a queue-policy change on the receiving side.

**The whitelist-by-PR pattern exists for one captain.** The decoy responder
role is generated from the enabled whitelist rows: each `ResponseAction`
declares `iam_statements(scope)` and `check_scope` (`captains/honeypot/actions/base.py:134-138`);
`responder_policy` is the union of the enabled rows' statements
(`captains/honeypot/responder_role.py:23-40`); `generate_responder_module`
renders a Terraform module (`services/_responder`, role `svc-ops-responder`
under `/service/`) with a trust policy on the control principals and an
external id (`:52-101`); the captain opens it as a PR
(`captains/honeypot/captain.py:436-479`); `/api/honeypots/actions/check`
asks IAM, with `simulate_principal_policy`, whether the applied role can do
what the rows need (`responder_role.py:103-117`, `honeypot_actions.py:131-155`).
The docstring states the rule this spec generalises: *"Nothing AWS shows
carries a product word."* What is missing: the whitelist row has `enabled`,
`mode` and `risk_max` but no notion of whether its grant has been merged and
applied, so a row can be `autopilot` while the role cannot do it (the action
then fails at execute, after the snapshot). Removal is an `enabled: false`
row; the permission stays in the role until someone opens the PR again.

**The gate.** `compass_core.policy.gate(mode, risk, risk_max)`
(`compass_core/policy.py:264-276`) decides `run`, `wait_for_approval` or
`recommend_only` from the row's mode and a deterministic risk; the model never
sets the risk (`captains/honeypot/policy.py:1-6`). Pending approvals are
parked on the incident and run from the dashboard with one click
(`approve_pending_action`, `respond.py:165-204`). The audit row is written before the first AWS call
and after each step (`captains/honeypot/responder.py:44-106`).

**What the daemon knows about a running job.** `serve._run_queued_jobs`
claims a job and runs it inside `job_event_context(job_id, workspace_id)`
(`compass_core/serve.py:244-257`), which sets two contextvars
(`compass_core/events.py:66-99`); `dispatch.dispatch(captain, command, payload)`
knows the captain (`compass_core/dispatch.py:29-39`) and sets nothing. The
API runs some AWS work inside the request (arm, check, rollback, validate)
with the signed-in user known from `get_actor_ids(request)`.

**The manifest has no permission field.** `CaptainManifest`
(`compass_core/captains.py:91-112`) declares commands, routers, tabs,
services, config schema and version. `compass validate`
(`compass_core/captain_validate.py`) checks packaging, entry points and the
manifest's own `errors()`. The catalog reviewer reads the code
(`docs/captain_development.md` §10).

**Tests and fakes.** The e2e stack points every AWS call but DynamoDB at moto
(`e2e/compose.e2e.yml:10-11`) with static credentials (`e2e/conftest.py:194-197`),
and seeds a seven-resource account (`e2e/fixture.py:56-78`). Unit tests mock
`build_target_session` or use `moto` (`tests/compass_core/test_core_setup.py:37-53`,
`tests/api/test_honeypot_actions.py:109`). The CLI's connect flow
(`cli/src/tools/account.ts:46-47`) posts to `/api/settings/aws`; its skill
(`cli/plugin/skills/compass-connect-account/SKILL.md`) describes the hosted
product's role template, quick-create link and `read-only` / `remediate`
tiers, which the open API does not have.

## 3. The model

```
 CONTROL (where Compass runs; any account, or the same one)   TARGET (what it watches)
 ┌──────────────────────────────────────────────┐             ┌──────────────────────────────────┐
 │ base identity  ── sts:AssumeRole only ──────────────────────▶ read role   (what captains read) │
 │   task role on AWS · runtime user elsewhere  │             │ act role    (armed actions only) │
 │ alert queue  ◀─────── sqs:SendMessage ───────────────────── alert_delivery roles, listeners   │
 │ dynamodb-local · the map file · config       │             │ (created by decoy modules, as now)│
 └──────────────────────────────────────────────┘             └──────────────────────────────────┘
      ▲ ambient credentials: the base identity, used for                roles created once by
        AssumeRole, the queue, Secrets Manager; nothing else             `compass connect`
```

Five rules, each enforced in code, not by convention:

1. **The profile is for setup.** `compass connect` runs as a one-off container
   with the profile mounted for that run. The services never mount `~/.aws`,
   `AWS_PROFILE` is not in their environment, `auth_mode: local_iam` and
   `COMPASS_ALLOW_LOCAL_IAM` are gone, and the broker has no code path that
   returns an ambient session for a target.
2. **One broker.** `compass_core.sessions.target(capability, …)` is the only
   thing that calls `sts:AssumeRole`; `compass_core.sessions.control()` is the
   only thing that hands out the base identity. A test fails the build when
   `boto3.Session(`, `boto3.client(` or `boto3.resource(` appear outside the
   allowlisted modules.
3. **Reads are narrowed per captain.** A read session's inline session policy
   is the captain's declared reads; STS intersects it with the role's policy.
   A captain cannot read more than it declared, even if the role allows it.
4. **Writes are actions, granted by PR.** The act role's policy is the union
   of *armed* actions. An act session's session policy is the one action
   being executed, and its `SourceIdentity` is the person who approved it. A
   row that is not armed never reaches `execute`, whatever its mode says.
5. **Compass checks its own access.** The grant is verified live before an
   action is armed, and a scheduled audit flags anything in either role that
   no manifest or grant explains.

### 3.1 Vocabulary

- **Base identity** — the credentials the Compass containers run with. On
  AWS: the task role or instance profile. Elsewhere: a runtime IAM user that
  `connect` creates, whose only policy is `sts:AssumeRole` on the two roles
  and `sqs:ReceiveMessage`/`DeleteMessage` on the alert queue (decision 1).
- **Read role** — the role in the target account every captain reads through.
  Default name `svc-inventory-reader-<install>` under `/service/` (decision 3).
  Every installation has its own pair of roles (D1); `<install>` is a short id
  `init_config` generates and writes as `aws.install_id`.
- **Act role** — the role whose policy is the union of armed actions. Today's
  `svc-ops-responder`, with the same `-<install>` suffix for new installs; an
  existing responder role keeps its name.
- **Capability** — `read` or `act`. `write` and `respond` go away: `write`
  was only `init_alerts`, which no longer writes into the target; `respond`
  is `act`.
- **Action** — a named write a captain may propose: an `action_type`, a scope,
  `iam_statements(scope)`, `check_scope`, a change type and a risk. Today's
  `ResponseAction` is the first kind; `compass_core.actions.Action` is the
  base every captain's actions subclass.
- **Grant** — one action's whitelist row and its lifecycle (section 9):
  `proposed → pr_open → merged → applied → verified → armed`, and on removal
  `disarmed → revoke_pr_open → revoke_merged → revoked`.
- **Session name** — `<prefix>-<captain>-<job>`; `<prefix>` is `compass` by
  default and configurable (`aws.session_prefix`), because the responder's
  cover-name rule applies to anything an attacker with CloudTrail access can
  read. `<job>` is the hex half of the job id, or `api-<8 chars of the request
  id>` for work done inside an API request.

## 4. `compass connect` (3.1)

One command, run with the operator's profile, idempotent, that leaves a target
account holding exactly two roles for this installation and the control side
holding one queue per region and, when the machine has no AWS role of its own,
one runtime user.

**Who owns the roles (D1).** The install method decides. An install through
the Terraform module or the CloudFormation stack (11.1, 11.3) creates the
roles in code and never runs `connect` against the target. A local install
runs `connect`, which creates the roles directly with the operator's profile.
Every installation has its own pair, so a laptop Compass next to a
Terraform-managed one creates its own roles and never touches the other's.
The self-audit (9.3) reports a role that no connected IaC repo holds as a
finding, "not under code", whose one action opens the PR that adds the access
module with `import` blocks (9.2). Once the role is in a repo, `connect` stops
writing it: a later run that needs to change the read policy opens or updates
the access-module PR instead, and says so. The finding shows on Settings →
Account → Access and in the inbox (4.7), which gets its own spec and ships
before PR 6.

```sh
docker compose run --rm -v ~/.aws:/root/.aws:ro -e AWS_PROFILE=<profile> \
    compass connect account=123456789012 region=us-east-1
```

What it does, in order, each step printed as it runs and skipped when already
done (a second run is a no-op that reports the state):

1. **Identify.** `sts:GetCallerIdentity` with the profile. Refuse if the
   profile is the runtime user from a previous run (it would be a sign the
   containers are being run with the profile).
2. **Control side.** Decide the control account: the account of the profile
   unless `control_account=` says otherwise (the same account as the target is
   fine). Create the alert queue there, `compass-decoy-alerts-<workspace>`
   as today (`commands.py:297-309`), with the policy and the SQS-managed
   encryption of section 6.
   **Base identity (D7).** Ask the machine for its own AWS role: IMDSv2 on
   EC2, the container credentials endpoint on ECS. If it has one, the roles
   trust it and no runtime user is created. `base_identity=<arn>` overrides
   the detection. An EC2 instance with an IMDSv2 hop limit of 1 hides its role
   from containers; `connect` says so and asks for `base_identity=`.
   Otherwise (decision 1), create the runtime user `svc-compass-runtime-<install>`
   under `/service/` with one inline policy (AssumeRole on the two role ARNs,
   receive/delete on the queue, `sts:GetCallerIdentity`) and one access key,
   written to `config/local.json` under `aws.control.credentials`. The file
   already holds the GitHub token and the dashboard password; it is the
   secrets file until 3.10 moves them.
3. **Target side.** Create the read role with the policy of section 5 and a
   trust policy on the base identity's ARN with the external id and
   `sts:SetSourceIdentity` allowed; create the act role with an **empty**
   policy and the same trust. Tag both `managed-by = <prefix>`. Record the
   external id as today (`settings.py:172-190`).
4. **Verify.** Assume both roles as the base identity (not as the profile):
   `GetCallerIdentity` on each, then `simulate_principal_policy` on the read
   role for every statement of its policy. A role created seconds ago may not
   be assumable yet (IAM is eventually consistent), so the first AssumeRole
   retries for up to 30 seconds. Print a table of denied actions, if any.
   Write `aws` in the config (section 12) with `account_id`, `connected_at`
   and the verification result. The account-id lookup moves here from
   `init_config` (`commands.py:147`), which has no role to read through (D4).
5. **Hand over.** Print what to do next: `docker compose up -d`, and that the
   profile is no longer needed. Queue the first map scan if the daemon is up.

`connect` runs again whenever the operator needs to: a new region to add to
the queue set, a new target account (`aws.targets[]`, one entry today), a
rotation of the runtime user's key (`connect rotate`), or to apply a new
captain's reads while the roles are not yet under code (once they are, it opens
the PR instead, D1).
It never touches the act role's policy: that is the grant flow's.

**No profile at hand.** `compass connect template account=… region=…` prints
the same two roles and the queue as a CloudFormation template with a
quick-create link and as a Terraform module, with the external id and base
identity ARN filled in, for an account the operator cannot reach from this
machine. `connect verify` then does step 4 alone. This is the hosted
product's flow (the CLI skill describes it) brought into the open edition.

**The startup check.** `serve` and `api` call `sessions.control()` once at
start and refuse to run when the ambient identity is a user whose ARN is not
`aws.control.runtime_arn`, or when `~/.aws` is mounted (`/root/.aws` exists),
or when `AWS_PROFILE` is set. The message names the fix in one line. A
target with no `aws.roles` yet (a fresh install before `connect`) starts and
shows the Connect account card; nothing that needs a target runs.

**Compose.** The `x-compass` anchor loses the `~/.aws` mount and
`AWS_PROFILE`; the `updater` service loses `HOME`. `.env.example` loses
`AWS_PROFILE`; `AWS_REGION` stays as the control region (DynamoDB-local does
not care; SES and Secrets Manager do). The quickstart's step 2 becomes
`init_config` → `init_tables` → `connect` → `up -d`; `init_alerts` is
absorbed into `connect`.

## 5. The read role (3.2)

The read policy is *generated*, from the installed captains' declared reads
(section 8), and the generator is also what the mapper's own manifest
declares. There is no hand-written policy to drift from the code.

```
read role policy  =  ∪ over installed captains of  manifest.permissions.aws.read
                     + the two statements every install needs (the `core` block, section 7):
                       sts:GetCallerIdentity
                       iam:GetRole, iam:GetRolePolicy, iam:ListRolePolicies,
                       iam:ListAttachedRolePolicies, iam:SimulatePrincipalPolicy
                         on the two Compass roles only (the self-audit, section 9.3)
```

What the first-party captains declare, from what their code calls today
(Appendix A has the full list per captain, by file):

| Captain | Declares | From |
|---|---|---|
| mapper | `ec2:Describe*`, `ecs:Describe*`/`List*`, `lambda:List*`, `iam:List*`/`Get*` (roles and policies), `s3:ListAllMyBuckets`/`GetBucket*`, `rds:Describe*`, `dynamodb:List*`/`Describe*`, `sqs:List*`/`GetQueueAttributes`, `sns:List*`, `events:List*`/`Describe*`, `states:List*`, `logs:Describe*`, `cloudwatch:Describe*`, `ecr:Describe*`, `elasticloadbalancing:Describe*`, `secretsmanager:List*`; plus `s3:GetObject`/`ListBucket` on the configured Terraform state bucket | `captains/mapper/aws_mapper.py`, `tf_state_graph.py` |
| honeypot | reads for `arm`, the investigation and the correlation: `iam:GetUser`, `ListAccessKeys`, `ListUserTags`, `iam:Get*Policy*`, `iam:List*RolePolicies`, `events:DescribeRule`, `ListTargetsByRule`, `cloudtrail:DescribeTrails`, `GetTrailStatus`, `LookupEvents`, `tag:GetResources`, `ecs:Describe*`/`List*`, `ec2:DescribeNetworkInterfaces`, `s3:ListAllMyBuckets`, `secretsmanager:ListSecrets`, `rds:DescribeDBInstances`, `dynamodb:ListTables` | `captains/honeypot/arm.py`, `investigate.py`, `correlation.py` |
| iac | the mapper's set (terraform plan refreshes what it imports) plus the state bucket | `captains/iac/import_job.py:105` |
| prs, librarian | nothing | — |

Wildcards per service (`ec2:Describe*`) rather than the 45 individual
actions, for two reasons: the session policy that narrows a captain (section
7) has a 2,048-character plaintext limit, and a new describe call in a
collector should not need an IAM change. Every wildcard is a read-only verb
prefix; `iam:Get*` and `iam:List*` are the broadest and are what the map is
for. `s3:GetObject` is scoped to the state bucket and never `*`: state files
hold secrets in plain text, and that is the one read a third-party captain
must not be able to borrow (section 7). `lambda:Get*` is left out for the same
reason (D5): `GetFunction` returns a link to download the function's code,
which often embeds secrets, and the mapper only lists. The validator refuses
`lambda:Get*` and `lambda:GetFunction` in any read block.

**Growth after setup.** Installing a captain whose declared reads are not
covered by the role's policy opens a PR that adds them to the access module
(section 9.2), the same way an action grant does; the Captains page shows
"waiting for the read policy" on the captain until the PR is applied and
verified, and the captain's jobs fail fast with that reason rather than with
`AccessDenied` from deep in a collector. While the roles are not under code,
re-running `compass connect` applies the change directly instead (D1).

## 6. Alerts flow into the control account (3.3)

The queue moves. `init_alerts` goes; `connect` creates the queue in the
control account, and its policy allows:

```json
{"Sid": "TargetAccountDelivery",
 "Effect": "Allow",
 "Principal": {"AWS": "arn:aws:iam::<target>:root"},
 "Action": "sqs:SendMessage",
 "Resource": "<queue arn>"}
```

plus the receive/delete statement for the base identity. Granting the target
account's root principal delegates *which* roles may send to the target
account's own IAM, where only the decoy modules' `alert_delivery` roles and
the listener's task role have `sqs:SendMessage` on that ARN
(`terraform_generator.py:137`). Nothing in the decoy modules changes: they
already send through a role to `var.alert_queue_arn`; the ARN now names a
queue in another account. EventBridge delivers to a cross-account SQS target
through the target's `role_arn`, which these modules already set.

**Encryption (D8).** The queues use SQS-managed encryption (SSE-SQS). It works
across accounts with no key policy and no change to the decoy modules. An
AWS-managed KMS key (`alias/aws/sqs`) cannot be used across accounts and would
drop every alert; a customer-managed key would need `kms:GenerateDataKey` on
every delivery role. `connect` sets `SqsManagedSseEnabled`, and the self-audit
flags a queue that carries a KMS key instead.

**Regions.** EventBridge cannot deliver to SQS across regions
(`alert_consumer.py:68-76`), so there is one queue per region the decoys live
in, as today (`alert_queue_urls`); `connect` creates them for the regions
given and the consumer polls each with `sessions.control()`. The queues'
region is the control account's; the rule's region is the target's; an
EventBridge target in region A may be an SQS queue in region A of another
account, so decoys in `eu-west-1` need a control queue in `eu-west-1`.
`connect` creates the queue in the target's region(s), in the control account.

**Migration.** An install with `honeypot.alert_queue_arn` pointing into the
target account keeps working until `connect` runs; `connect` creates the new
queue, rewrites `alert_queue_arn`/`alert_queue_url(s)` to it, and prints
that decoys planted before this point still report to the old queue until
their module's `alert_queue_arn` variable is updated, which `honeypot
replant-alerts` does as one PR touching only that variable in each module.
The old queue is the operator's to delete.

**The listener image.** `db_credential` listeners run as an ECS task in the
target account and send with the task role; unchanged.

## 7. The session broker (3.6)

`compass_core/sessions.py` replaces `compass_core/aws.py`'s session
construction (`aws.py` keeps the config readers).

```python
Capability = Literal["read", "act"]

def control(*, region: str | None = None) -> boto3.Session:
    """The base identity. For the control account's own resources only:
    AssumeRole, the alert queues, DynamoDB, Secrets Manager, SES."""

def target(
    capability: Capability,
    *,
    region: str | None = None,
    captain: str | None = None,      # default: the current captain (contextvar set by dispatch)
    job_id: str | None = None,       # default: events.current_job_id(), or the API request id
    approver: str | None = None,     # act only; the person whose approval runs this action
    action: Grant | None = None,     # act only; the one action this session may perform
    config: Mapping | None = None,   # default: the current workspace config
) -> boto3.Session:
    """A role session in the target account, named, attributed and narrowed."""

def target_env(capability: Capability, **kw) -> Dict[str, Optional[str]]:
    """target(...) as environment variables for terraform (today's target_credentials_env)."""

def set_session_factory(factory) -> None:
    """The seam: a host that brokers credentials differently installs its own."""
```

**What `target` does.**

1. Resolve the captain: the argument, else `dispatch`'s contextvar (set in
   `dispatch.dispatch` for the handler's duration), else `api` for a request
   (the API sets it from the route's captain, known from the manifest's
   `route_prefixes`); Compass's own calls pass `captain="core"` (D4). No
   captain → `ValueError`; a session is never anonymous.
2. Resolve the job: the argument, else `events.current_job_id()`, else the
   request id. Session name `<prefix>-<captain>-<job>`, validated against
   STS's `[\w+=,.@-]{2,64}`.
3. `read`: `RoleArn = aws.roles.read`; `Policy =` the captain's declared read
   statements (section 8), rendered to JSON; if the render exceeds 2,048
   characters, `PolicyArns = [the captain's managed policy]` instead (section
   8, "large declarations"). `DurationSeconds` 3600; read sessions are cached per
   `(captain, job)` and refreshed 5 minutes before expiry, so a scan does not
   assume once per region.
4. `act`: `RoleArn = aws.roles.act`; `Policy =` the one action's
   `iam_statements(scope)`; `SourceIdentity = approver` (required: an act
   session without an approver is refused in the broker, not at the gate).
   Refused when the grant is not `armed` (section 9), before any STS call.
   Act sessions are never cached (D3): every action assumes the role afresh,
   so two actions in one job never share a session and each CloudTrail entry
   maps to one action and one approval.
5. `ExternalId = aws.external_id` on every call. The STS client is
   `control()`'s.
6. The returned session is tagged (a private attribute) with its name,
   capability and policy hash, so the audit row of an action (`responder.py:65`)
   can record the exact session that ran it.

**The `core` identity (D4).** Calls that belong to Compass and not to a
captain run as `core`: the settings page's account check (`settings.py:140`,
`:241`), `connect verify` and the self-audit. Its read block is the two
statements every install needs (section 5). CloudTrail shows these sessions as
`compass-core-<job>`.

**Why a session policy and not a role per captain.** One AssumeRole, one role
to create, one trust policy, and STS does the intersection: a captain whose
manifest lists only `ecs:Describe*` gets a session that fails
`s3:GetObject` on the state bucket with `AccessDenied` from AWS, whatever the
role allows. A role per captain would need `connect` or a PR on every install
and a trust policy per role; the session policy needs neither. The act role
works the same way: its policy is the union of armed actions, and each
session is narrowed to the one action running.

**`SourceIdentity`.** Set once at AssumeRole, it cannot be changed by the
session and is written to every CloudTrail event the session makes, so the
target account's trail shows `sourceIdentity: ilia@example.com` on the
`UpdateAccessKey` call. The trust policy must allow `sts:SetSourceIdentity`
(`connect` writes it). Read sessions set it too, to the job's requester when
there is one (the person who clicked "Run mapping"), else `scheduler`.
The value is the person's email (D6). For a job it comes from the actor ids
the jobs route already merges into the payload (`compass_api/routes/jobs.py:73-77`);
for in-request work, from `get_actor_ids(request)`. Characters outside
`[\w+=,.@-]` become `-` and the value is cut to 64. Anyone with CloudTrail
access in the target account can read it; `docs/access.md` says so.

**The rule, enforced.** `tests/compass_core/test_sessions_only.py` walks
every `.py` under `compass_core`, `compass_api`, `captains` and `infra_map`
and fails on `boto3.Session(`, `boto3.client(`, `boto3.resource(` or
`botocore.session` outside `compass_core/sessions.py`. The 23 ambient sites
of section 2 move to `sessions.control()`; the 12 target sites to
`sessions.target(...)`; the mapper's regional re-session (`aws_mapper.py:1417`)
and the arm route's re-pin (`honeypots.py:939`) become
`sessions.target("read", region=…)` from the cache. `captains/honeypot/listener/`
is allowlisted: the listener runs as an ECS task in the target account with
its own task role and no Compass config, so there is no broker to call. `ruff` cannot
express this rule; the test is three lines of `ast` and one allowlist.

**Cloud neutrality.** `target` returns `Any` typed as the provider's session;
`Capability` and the signature carry nothing AWS-specific. `sessions.py`
dispatches on `aws.provider` (absent = `aws`) to `compass_core/providers/aws.py`,
which is where `AssumeRole`, session policies and `SourceIdentity` live. An
Azure provider would return a `TokenCredential` for one managed identity per
captain (Azure has no session policies; the narrowing is the identity's role
assignments), and the manifest's `permissions.azure` would list role
definitions. None of that is built; the split exists so it can be.

## 8. Permissions in the manifest (3.4)

```python
MANIFEST = CaptainManifest(
    name="honeypot",
    ...
    permissions=Permissions(
        aws=AwsPermissions(
            read=(                       # statements of the read session policy
                Statement(actions=("iam:GetUser", "iam:ListAccessKeys", "iam:ListUserTags",
                                   "events:DescribeRule", "events:ListTargetsByRule",
                                   "cloudtrail:DescribeTrails", "cloudtrail:GetTrailStatus",
                                   "cloudtrail:LookupEvents", "tag:GetResources",
                                   "ecs:Describe*", "ecs:List*", "s3:ListAllMyBuckets",
                                   "iam:GetPolicy", "iam:GetPolicyVersion", "iam:GetRolePolicy")),
            ),
            actions=("iam_deactivate_key", "ecs_restart_task"),   # action types it may propose
        ),
        github=("pull_requests:write", "contents:write"),         # informative today; the GitHub client checks nothing
        egress=("api.github.com",),                               # informative today
    ),
)
```

- `Statement(actions, resources="*", conditions=None)`. `resources` may use
  `{state_bucket}` and `{account_id}`, filled from the config at render time.
- `actions` names action types registered by the captain's `actions` module
  (today `captains.honeypot.actions.get_action`; it moves to
  `compass_core.actions.register(captain, {type: Action})` beside
  `dispatch.register`). An action type is `<captain>.<name>` once outside
  the honeypot.
- `errors()` checks: every read action matches `^[a-z0-9-]+:[A-Za-z*]+$`;
  no read verb outside `Get|List|Describe|Lookup|Simulate|Search|Query|Scan|
  BatchGet|Select|Check|Test|View|Report|Discover|Retrieve|Export|Generate
  Credential` prefixes (the IAM read-only verb set; a captain that needs a
  write declares an action); no `*:*`; `s3:GetObject` not on `*`; no
  `lambda:Get*` or `lambda:GetFunction` (D5); every
  named action type is registered; the rendered read policy is under 6,144
  characters (the managed-policy limit, for the fallback).
- `compass validate` adds two checks (the static pass ships in PR 2 as a test
  over the first-party captains, D9; PR 4 puts it in `compass validate`): the manifest's permissions render to a
  valid policy, and **the code uses only what it declares**: a static pass
  over the package for `.client("<service>")` and `<verb>_<noun>(` pairs
  (the same grep Appendix A was made with, as a function), mapped to IAM
  action names through botocore's service model (`operation_name` → the
  `iam:Action` is the operation name in almost every service; a short table
  covers the exceptions such as `tag:GetResources`). A call the manifest does
  not cover is a validation failure naming the file and line. This is what
  lets the catalog reviewer read a list instead of the code (5.5).
- **The Captains page** shows the permissions block before install under
  "Will use": reads grouped by service, actions with their change type and
  rollback, GitHub scopes, egress. A captain with `actions` says "can propose
  N write actions; each needs your approval and a merged pull request before
  it can run".
- **Large declarations.** A read block that renders over 2,048 characters as
  a session policy cannot be passed inline. `connect` (or the install PR)
  then creates a managed policy `svc-inventory-reader-<captain>` in the
  target account from the block, and the broker passes its ARN as the
  session's `PolicyArns`. No first-party captain needs this; the mapper's
  block renders to about 900 characters with wildcards.
- Catalog entries carry the permissions block so the Available list shows it
  before the package is even downloaded (`compass captain publish` copies it
  from the manifest).

## 9. Writes granted by pull request (3.5), the lifecycle (3.11), the self-audit (3.12)

### 9.1 The grant and its states

A grant is one whitelist row, generalised from `captains/honeypot/respond.py`'s
action rows to every captain, stored where they are today (the items table,
`HONEYPOT#ACTION#` keys, from `honeypot_action_prefix` in
`compass_core/keys.py:182`, become `GRANT#<captain>#<action_type>#<id>`; a
migration step renames the honeypot's rows). Fields: `captain`, `action_type`,
`scope`, `mode` (`off` / `approve` / `autopilot`), `risk_max`, and new:
`grant_status`, `grant_pr` (URL), `applied_at`, `verified_at`,
`policy_hash` (of the statements the PR granted).

```
proposed ─(accept)─▶ pr_open ─(merge)─▶ merged ─(statements in IAM)─▶ applied ─(simulate ok)─▶ verified ─▶ armed
                                                                                                             │
                                                                                               (person removes)
revoked ◀─(gone from IAM, simulate denies)─ revoke_merged ◀─(merge)─ revoke_pr_open ◀───────────── disarmed ◀┘
```

- **proposed**: Compass (a captain, or the catalog install) proposes an
  action with a scope. The dashboard shows it under Settings → Account →
  Access with what it would allow, in IAM terms and in words.
- **pr_open**: a person accepted it (chose `approve` or `autopilot`). Compass
  opens the access-module PR (9.2) adding the action's statements to the act
  role. The row cannot run.
- **merged**: the PR watcher saw the merge, as it does for decoy plants.
  Nothing in AWS has changed yet; the row shows "merged, waiting for apply".
  The team's own process runs `terraform apply`: on merge, by hand or on a
  schedule.
- **applied**: the tick reads the act role's policy back from IAM and finds
  the grant's statements in it. A merge is never taken as an apply (D2).
- **verified**: `simulate_principal_policy` on the act role, for the action's
  statements against the scope's resources, answers `allowed` for every one.
  The verification runs on a service tick (every 5 minutes while any grant
  is `pr_open`, `merged`, `applied`, `revoke_pr_open` or `revoke_merged`; a
  "Verify now" button runs it in the request).
- **armed**: the gate may return `run` for it. `respond` and every other
  executor check `grant_status == armed` before `policy.gate` and before the
  snapshot; a row that is not armed yields `wait_for_grant`, shown as such on
  the incident. Autopilot on an unarmed row runs nothing.
- **disarmed**: a person removed the action (or set it `off`). Immediate; no
  AWS call. Compass opens the revoke PR in the same step.
- **revoke_pr_open → revoke_merged → revoked**: the PR removes the
  statements; `revoked` only once IAM no longer holds them and simulation
  denies. Until then the row shows "permission still
  granted in AWS, removal waiting on <PR>".
- A grant whose statements change (a wider scope) goes back to `proposed`
  with the old grant kept `armed` until the new PR applies, so widening never
  disarms what works.

**What this means in an incident.** Response actions must be armed before a
decoy fires; nobody is merging IAM PRs mid-incident. The dashboard says so on
the Decoys settings ("2 of 3 responses armed; `ecs_restart_task` waiting on
#42"). This is the same constraint the honeypot has today, made visible.

### 9.2 The access module

`compass_core/access_module.py` generalises `captains/honeypot/responder_role.py`:
one Terraform module, `services/_ops_access/` (decision 3 on names), with
`main.tf` holding the read role, the act role, their trust policies and the
act role's policy as the union of the grants in `pr_open`, `merged`,
`applied`, `verified` or `armed`, plus the still-armed old row of a grant
being widened. A `disarmed` grant is left out, which is what makes the
regenerated module the revoke PR. Every PR regenerates the whole module from the grant
table and the manifests, so a PR is always "the module as it should be now",
never a hand-merged diff, and two grants proposed in the same minute land in
one PR. The read role's policy is in the same module (section 5), so a
captain install and an action grant are the same kind of PR.

`connect` writes the module's *initial* state directly (section 4) and, when
the workspace has an IaC repo, also opens the PR that adds the module to it
with `import` blocks for the two roles, so the first grant PR is a diff on an
existing module rather than a new directory. Until that PR is merged and
applied the roles are not under code, and the self-audit says so (D1). Without an IaC repo configured, grants cannot be made: the
Access page says "connect a repository to enable write actions", and
`connect` says the same.

The PR body lists, per grant, the action in words, its scope, the IAM
statements, the risk and change type, and who accepted it. The PR is opened
by the GitHub identity Compass already uses; the branch prefix stays
`beamreach/` (the namespace note in CLAUDE.md).

### 9.3 The self-audit (3.12)

A core service, `access-audit`, every 24 hours and on demand:

1. With a read session (the two IAM statements of section 5 are scoped to
   the two roles): fetch both roles' trust policies, inline policies and
   attached policies.
2. Compute the expected state: read policy from the installed manifests, act
   policy from the grants that should be granted, trust from the config.
3. Diff. Anything in AWS that the expectation does not explain (a statement
   added in the console, an attached managed policy, a second trusted
   principal) is a **finding** with the statement and when it was first
   seen; anything expected but missing is a finding of the other kind (a
   grant that says `armed` but is not in the role → the grant drops to
   `applied` and re-verifies). A role that no connected IaC repo contains is
   a third kind, "not under code", whose action is the import PR of 9.2 (D1).
4. Findings show on Settings → Account → Access and in the inbox (4.7, its
   own spec, shipped before this PR); one
   button opens the PR that regenerates the module (which removes what the
   module does not contain, since Terraform owns the role), and the finding
   stays until the next audit confirms the role matches.

The audit writes one row per run (`ACCESS#AUDIT#<ts>`) with the diff, so
"when did this appear" is answerable later.

## 10. The rest of the angle, briefly

**3.7 Out-of-process third-party captains.** A worker is `compass serve
--captains=<name>` in its own container with `sessions.set_session_factory`
pointed at the daemon's broker over the API (`POST /api/internal/sessions`,
admin token, returns a session's temporary credentials for one `(captain,
job, capability)` after the same checks). The worker has no Docker socket, no
DynamoDB credentials beyond its jobs, no `config/local.json`. The broker's
API shape is all this spec fixes.

**3.8 Untrusted input to the model.** Prompts that include PR comments,
resource tags, decoy event payloads or CloudTrail fields wrap them in a
`<data>` block with a one-line instruction that the block is data; model
output selects nothing: today's `choose_actions` picks among rows by the
LLM only when `HONEYPOT_AGENT_CHOICE=llm` (`respond.py:219-224`), and the
pick is then re-checked against the row list and the gate. The rule becomes
a test: no `policy.gate` input and no `grant_status` transition is ever a
model output.

**3.9 One policy engine.** The honeypot gate and the PR watcher's gate are
already one function (`policy.gate`); what differs is where the mode and
`risk_max` come from. A later change reads them from one `policy` section
with rules (two-person approval above a risk, quiet hours, a token budget
per captain) and keeps the function.

**3.10 Audit and secrets.** The action audit rows stay; job events move off
the job item (8.1) first. The runtime user's key, the GitHub token and the
model keys move from `config/local.json` to the credentials store (6.6) when
it exists; `sessions.control()` reads from there through the same config
reader.

## 11. The base identity off AWS (decision 1)

The containers need *some* AWS identity to call `AssumeRole`. On ECS or EC2
it is the task role or instance profile, created by the deployment stack
(8.5) with the assume-only policy; nothing is stored. `connect` finds that role
itself and `base_identity=` overrides it (section 4, step 2, D7). Elsewhere,
the choices:

- **(a) A runtime IAM user**, created by `connect`, with one inline policy:
  `sts:AssumeRole` on the two roles, `sts:GetCallerIdentity`,
  `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes` on the alert
  queues, and, when the install uses them, Secrets Manager and SES in the
  control account. Its access key lives in `config/local.json`. Blast radius
  if the key leaks: assume the read role (read the account) and drain the
  alert queue; the act role is usable only with `SourceIdentity` set by a
  caller that knows the external id, which is in the same file, so a leaked
  config file is a leaked act capability for the armed actions. `connect
  rotate` rotates the key; the audit flags a key older than 90 days.
- **(b) IAM Roles Anywhere**: a private CA, a trust anchor and a profile; the
  containers hold a certificate and the `aws_signing_helper` process serves
  credentials. No long-lived key; a leaked config file holds no AWS
  credential. Costs a CA to run and a helper binary in the image; the e2e
  suite needs a fake for it.
- **(c) Require AWS**: no off-AWS install; the quickstart deploys the control
  stack (8.5) first.

Recommendation: **(a)** as the default, (b) documented as the hardened
option with the same `aws.control` config shape (`credential_source:
access_key | roles_anywhere | ambient`), (c) rejected because the laptop
install is how people try Compass.

## 12. What changes where

**Config (`config/local.json`).** The `aws` block becomes:

```json
"aws": {
  "provider": "aws",
  "install_id": "k3f9a2",
  "account_id": "123456789012",
  "region": "us-east-1",
  "regions": ["us-east-1", "eu-west-1"],
  "roles": {"read": "arn:aws:iam::123456789012:role/service/svc-inventory-reader-k3f9a2",
            "act":  "arn:aws:iam::123456789012:role/service/svc-ops-responder-k3f9a2"},
  "external_id": "…",
  "session_prefix": "compass",
  "control": {"account_id": "123456789012",
              "credential_source": "access_key",
              "runtime_arn": "arn:aws:iam::123456789012:user/service/svc-compass-runtime-k3f9a2",
              "credentials": {"access_key_id": "…", "secret_access_key": "…"},
              "alert_queues": {"us-east-1": {"arn": "…", "url": "…"}}},
  "connected_at": "2026-10-19T20:30:00Z",
  "verified_at": "2026-10-19T20:30:04Z"
}
```

Gone: `auth_mode`, `assume_role`, `assume_role_write`, `assume_role_respond`,
`credentials*`, `generated_external_id`; `honeypot.alert_queue_*` is read
from `aws.control.alert_queues` with the old keys honoured for one release.
Migration step `0.7.0-access-model`: an install with `assume_role_respond`
gets `roles.act` from it; an install with `assume_role` gets `roles.read`;
an install with `local_iam` gets no roles and the Connect account card.

**API.** `POST /api/settings/aws` loses `local_iam` and `access_key`;
`assume_role` stays for `connect verify`'s result. New: `GET /api/access`
(roles, verification, grants with states, findings, last audit),
`POST /api/access/grants` (propose), `POST /api/access/grants/{id}/accept`
(→ `pr_open`, opens the PR), `/decline`, `/remove` (→ `disarmed`, opens the
revoke PR), `POST /api/access/verify`, `POST /api/access/audit`. The
honeypot's `/api/honeypots/actions/*` keep their paths and call the grant
layer; `/check` is `verify` for the honeypot's grants. All under the admin
write scope (`compass_api/auth/scopes.py`).

**CLI (`cli/src/tools/account.ts`, `compass account`).** `connect` prints
the `docker compose run … compass connect` line (or runs it when Docker is
reachable), `template`, `verify`, `rotate`. New `compass access` tool:
`list`, `accept <id>`, `remove <id>`, `verify`, `audit`. The MCP server
exposes `compass_access_list` and `compass_access_verify` (read) and
`compass_access_accept`/`remove` (operate). `compass status` gains one line:
"Account 123456789012 · read ✓ · 2 of 3 actions armed · audited 3h ago".
`tests/compass_core/test_core_cli.py` pins the new commands: `connect`,
`access`.

**Skills.** `.claude/skills/compass-open/SKILL.md`: step 2 gains `connect`
before `up -d` and loses `AWS_PROFILE` in `.env`; step 3 loses `init_alerts`;
a note that the profile is used once. `cli/plugin/skills/compass-connect-account/SKILL.md`
is rewritten for the open flow (connect, template, verify) and loses the
hosted tiers. It is broken today: it calls `compass_account_role_template`,
which the open CLI does not have; `compass-status` mentions the access line; `compass-arm` says a
response must be armed (a merged PR) before it runs.

**Docs.** `docs/quickstart.md`: steps 2–3, "Your credentials, your account"
becomes "Compass reads your account through a role it creates once; it writes
nothing without a pull request you merge". `docs/architecture.md`: the
diagram's `AWS account (read; decoys write once)` line, a new "Access"
section, the seams table gains `Sessions | compass_core.sessions | set_session_factory`.
New `docs/access.md`: the two roles and their generated policies, `connect`,
the grant lifecycle with the state diagram, the self-audit, the runtime
identity options, how to read the target's CloudTrail (session names,
`sourceIdentity`). `docs/writing_a_captain.md`: §permissions, with the
validator's rule. `docs/captain_development.md` §10: the reviewer reads the
permissions block. `docs/e2e.md`: the connect step in the fixture.
`CHANGELOG.md`: `## 0.7.0 — unreleased`, "After the update": run `compass
connect` once; `init_alerts` is gone; decoys planted before 0.7 report to the
old queue until `honeypot replant-alerts`.

**Compose and image.** The `~/.aws` mount, `AWS_PROFILE` and `HOME` out;
`COMPASS_ALLOW_LOCAL_IAM` out of the Dockerfile. `.env.example` loses
`AWS_PROFILE`.

**Dashboard.** Settings → Account: the Connect card (state, the command to
run, verification result), the Access section (roles, grants with their
state and PR link, findings, Verify now / Audit now). Decoys settings: the
response rows show their grant state. `npx tsc --noEmit`.

## 13. Test strategy

- **The rule** (`tests/compass_core/test_sessions_only.py`): AST walk, fails
  on any `boto3`/`botocore` session construction outside `sessions.py` and
  `providers/aws.py`. Lands in the first PR, so every later PR is held to it.
- **The broker** (`tests/compass_core/test_sessions.py`, moto): `target("read")`
  assumes the read role with the right name, external id, `SourceIdentity`
  and session policy; a captain's session policy is exactly its manifest's
  render; `target("act")` without an approver is refused; `act` on an
  unarmed grant is refused before STS; the cache refreshes before expiry;
  the name is valid for a 30-character captain and a job id; no captain in
  context → `ValueError`; two actions in one job assume the act role twice
  (D3); `SourceIdentity` is the requester's email with disallowed characters
  replaced (D6); `core` sessions carry only the core block (D4). moto 5.2.4's STS reads `Policy` and ignores
  `SourceIdentity`, and evaluates neither, so the parameters the broker
  sends are asserted with a botocore `Stubber` on the STS client, and **the
  narrowing itself is tested by construction** (the policy passed) and once,
  live, in Phase 2's checklist against a real account.
- **`connect`** (`tests/compass_core/test_connect.py`, moto): a fresh account
  gets two roles, a queue and (off AWS) a runtime user with the assume-only
  policy; a second run changes nothing and reports; the trust policy carries
  `sts:SetSourceIdentity` and the external id; the config is written as
  section 12; `template` renders CloudFormation that `cfn-lint` accepts (a
  dev dependency, stated in the PR); the startup check refuses `AWS_PROFILE`
  and an unexpected user ARN; a fake metadata endpoint with a role means no
  runtime user is created, and `base_identity=` overrides it (D7). moto 5.2.4 does not implement
  `SimulatePrincipalPolicy` (the only mention in its tree is a data file),
  so the verify step is tested through a fake IAM client that records the
  call and answers `allowed` / `implicitDeny` from a table (today's `/check`
  test covers only the unconfigured path, `tests/api/test_honeypot_actions.py:98-99`);
  the real evaluation is in the Phase 2 checklist.
- **The read policy** (`tests/captains/test_permissions.py`): every
  first-party manifest's permissions validate; the validator's static pass
  finds every `.client(...)`/call in `captains/*` covered by its manifest
  (this is the test that keeps Appendix A true, and it lands in PR 2, D9); a
  manifest with a write verb in `read`, `s3:GetObject` on `*` or
  `lambda:Get*`, fails.
- **Grants** (`tests/compass_core/test_grants.py`, moto): the state machine
  including widening; a merged PR whose statements are absent from IAM stays
  `merged` (D2); the access module renders the union of the right grants; the executor refuses an unarmed row with `wait_for_grant`; the
  honeypot's existing responder tests move onto grants unchanged in what
  they assert.
- **Alert forwarding** (`tests/captains/honeypot/test_alert_queue.py`, moto):
  the queue policy allows the target root principal and nothing else to
  send; the queue has SSE-SQS and no KMS key (D8); the consumer polls with
  `control()`; a message from a cross-account delivery role is recorded.
- **The self-audit**: a role with an extra statement yields one finding; a
  missing grant statement drops the grant to `applied`; a role that no
  connected repo holds yields "not under code" (D1).
- **e2e** (`e2e/test_11_access.py`): the fixture runs `compass connect`
  against moto before `up` (`e2e/conftest.py` gains the step; static moto
  credentials are the "profile"), the map scan runs through the read role
  (moto's CloudTrail does not record session names; the test asserts through
  the audit row's session name instead), a grant goes `proposed → pr_open`
  with a PR on the fake GitHub, merging it in the fake and running verify
  arms it, and an incident response runs with `SourceIdentity` recorded in
  the audit. The existing decoy and IaC shards keep passing with the roles in
  place.
- **Dashboard**: `tsc`; the Account settings in the e2e shard A.

## 14. Risks

- **A breaking change for every install.** 0.7 refuses to run the old way.
  Mitigation: the migration step converts `assume_role*` configs with no
  action needed; `local_iam` installs get a one-line startup message and the
  Connect card, and the changelog's "After the update" is one command.
  Decision 5 is whether to give one release of grace instead.
- **Session policy size.** 2,048 characters plaintext, and STS packs it;
  the mapper's wildcard block is ~900. A captain that declares many
  resource-scoped statements hits it; the managed-policy fallback (section
  8) covers it, at the cost of an IAM object per such captain.
- **`SimulatePrincipalPolicy` is not execution.** It evaluates identity and
  resource policies it is given; SCPs and permission boundaries are
  evaluated only when the caller passes them. A grant can be `verified` and
  still be denied by an SCP. The action's `execute` keeps its own failure
  path and the audit row records the denial; the Access page shows "verified
  by simulation" rather than "will work".
- **Cross-account EventBridge → SQS.** Standard, but one more thing per
  region; the `arm` check already verifies rule and target and gains a
  `send a test event through the rule` step so a misconfigured queue policy
  is found at arm time, not at the first real trip.
- **The runtime user's key on disk.** Decision 1 states the blast radius;
  rotation and the audit's age check bound it; Roles Anywhere is the answer
  for anyone who cannot accept it.
- **The in-request writes.** The API runs `arm`, `check` and `rollback`
  inside the request with the signed-in user as approver. Fine for
  attribution; the request can outlive a browser timeout. Unchanged from
  today, noted so 3.7's worker design moves them to jobs.
- **The e2e suite's moto.** moto 5.2.4's STS, IAM and SQS cover what
  `connect` and the broker call, with two gaps: `SimulatePrincipalPolicy` is
  absent and `SourceIdentity` is ignored. `connect verify` against moto
  therefore reports "not simulated" rather than failing, the e2e grant test
  arms through the fake's answer, and both are on the live checklist. If a
  later moto adds them, the fakes come out.
- **Cover names versus clarity.** `svc-inventory-reader` tells an attacker
  less than `CompassReadRole`; it also tells an operator less. The role tags
  carry `managed-by`, the docs say the names, and `compass status` prints
  them.

## 15. PR plan

Each is shippable alone, in this order, with a stop after each.

1. **`core: one session broker builds every AWS session`** (3.6, part 1).
   `compass_core/sessions.py` with `control()`, `target()`, `target_env()`,
   the captain contextvar in `dispatch`, the API request id as the job for
   in-request work, session names and `SourceIdentity`, the cache, and the
   AST test, the `core` identity, act sessions never cached. Every call site
   moves: about 38 (32 `boto3` constructions and 13 broker callers, some on
   the same line). **Behaviour otherwise
   unchanged**: `local_iam` still works *through the broker* (the ambient
   path is the one thing `target` still allows, behind the same flag), so
   this is a refactor that only adds names to sessions. *Stop: diff summary,
   the test, and a CloudTrail excerpt from a real account showing the new
   session names.*
2. **`core: compass connect creates the roles; the profile is for setup only`**
   (3.1, 3.2, the base identity). `connect` and `connect template|verify|rotate`,
   the generated read policy from the first-party manifests' read blocks
   (the `permissions` field lands here, read-only, with the validator's
   checks, and the static manifest-versus-code pass as a test over the
   first-party captains, D9; `actions` comes in PR 4), base-identity
   detection, the per-install role names, the account-id lookup moved out of
   `init_config`, the runtime user, the startup check, the
   config shape and migration step, compose, `.env.example`, Dockerfile, the
   skills, docs, `CHANGELOG` 0.7.0. The ambient path is removed from the
   broker in this PR. *Stop: a fresh install through the quickstart against a
   real account, the verification table, and the checklist of section 16.*
3. **`honeypot: decoy alerts flow into the control account`** (3.3). The
   queue in `connect` with SSE-SQS, the cross-account policy, the consumer on
   `control()`, `replant-alerts`, the arm-time test event, `init_alerts`
   removed from the CLI table. *Stop: a decoy fired in the target account
   recorded through the control queue.*
4. **`captains: permissions declare actions; validate checks the code against the manifest`**
   (3.4 complete). `actions` in `Permissions`, `compass_core.actions`, the
   static pass (a test since PR 2) in `compass validate` for third-party packages, the Captains page's "Will use", the
   catalog entry field. *Stop.*
5. **`core: write access by pull request, per whitelisted action`** (3.5,
   3.11). The grant table and states (with `merged` and `revoke_merged`,
   detected by reading the role back), the access module generalised from the
   responder role, accept/remove/verify, the executor's `wait_for_grant`,
   the `act` session narrowed to one action, the Access page, `compass
   access`. The honeypot's rows migrate. *Stop: a grant taken from proposed
   to armed on a real account, the PR, and the CloudTrail event with
   `sourceIdentity`.*
6. **`core: Compass audits its own access`** (3.12). The service, findings,
   the regenerate-PR button, the "not under code" finding and its import PR,
   `compass access audit`. Needs the inbox (4.7, its own spec). *Stop.*

3.8, 3.9, 3.10 and 3.7 follow as their own specs when they move up the board.

## 16. What to check by hand before PR 2 merges

Against one real AWS account, with the e2e world not involved:

- [ ] `connect` on a fresh account: two roles, one queue, one runtime user;
      a second run prints "nothing to do".
- [ ] The containers start with no `~/.aws` and no `AWS_PROFILE`; with
      either present they refuse with the one-line message.
- [ ] A map scan completes; CloudTrail in the target shows
      `compass-mapper-<job>` with `sourceIdentity` set, on `AssumeRole` and
      on a `DescribeInstances`.
- [ ] A read session for a captain declaring only `ecs:*` reads is denied
      `s3:GetObject` on the state bucket (the session policy works).
- [ ] The read role's policy, read back from IAM, equals the generated one.
- [ ] `simulate_principal_policy` for the read policy returns no denials.
- [ ] Nothing in the target account was written other than the two roles.
- [ ] A mapper session is denied `lambda:GetFunction`.
- [ ] `connect` on an EC2 host with an instance role trusts that role and
      creates no runtime user.
- [ ] A second local install in the same account creates its own pair of
      roles and leaves the first pair alone.

## Decided 2026-10-20

**D1. Who owns the roles.** The install method decides. Terraform or
CloudFormation installs own the roles in code; a local install's `connect`
creates them directly. Every installation has its own pair. A role not in any
connected repo is a self-audit finding, "not under code", with an import PR as
its fix. Once a role is in code, `connect` opens a PR instead of writing.
Supersedes open decision 4. Sections 4, 5, 9.2, 9.3.

**D2. Applied means applied.** A merged grant PR moves the grant to `merged`;
it becomes `applied` only when reading the role back from IAM finds the
statements. Revocation mirrors it with `revoke_merged`. Section 9.1.

**D3. Act sessions are never cached.** Read sessions stay cached per captain
and job. Section 7.

**D4. A `core` identity.** Compass's own target calls run as captain `core`
with the two base statements; the account-id lookup moves from `init_config`
into `connect`. Sections 4, 7.

**D5. No `lambda:Get*`.** The mapper declares `lambda:List*` only; the
validator refuses `lambda:Get*` and `lambda:GetFunction`. Sections 5, 8.

**D6. `SourceIdentity` is the email.** Sanitised to STS's character set;
background jobs use `scheduler`. Section 7.

**D7. `connect` finds the machine's role.** IMDSv2 or the ECS endpoint;
`base_identity=` overrides; a runtime user only when neither gives a role.
Sections 4, 11.

**D8. SSE-SQS on the alert queues.** No KMS key; the audit flags one.
Section 6.

**D9. The manifest-versus-code check lands in PR 2**, as a test over the
first-party captains; PR 4 adds it to `compass validate`. Sections 8, 15.

Item 4.7 (the inbox) gets its own spec and ships before PR 6.

## Open decisions

**1. The base identity off AWS.** (a) a runtime IAM user with an
assume-only policy, created by `connect`; (b) IAM Roles Anywhere; (c) no
off-AWS install. *Recommendation: (a) as the default, (b) documented with the
same config shape.* Section 11 has the trade-off.

**2. How `connect` applies the roles.** (a) directly with boto3 from a
one-off container that mounts the profile for that run; (b) `terraform
apply` with local state in `./config`; (c) only print a CloudFormation
quick-create link and a module, never touch the account from this machine.
*Recommendation: (a), with (c) as `connect template` for accounts the
operator cannot reach from here.* (b) adds a state file to protect and a
second way the roles can drift; (c) alone makes the quickstart a two-console
exercise.

**3. Role and module names.** Whatever the names, each carries the
installation suffix (D1). (a) cover names under `/service/`
(`svc-inventory-reader`, `svc-ops-responder`, module `services/_ops_access`),
the responder's existing rule; (b) product names (`CompassRead`,
`CompassAct`); (c) operator-chosen at `connect` with (a) as default.
*Recommendation: (c).* The rule exists because the decoys' value drops if
IAM announces the deception tooling; an operator who does not care picks a
clear name.

**4. Read-role growth after setup.** *Superseded by D1.* (a) a PR by default, `connect` re-run
allowed; (b) PR only; (c) `connect` only. *Recommendation: (a).* A captain
install is a code change to what Compass may read and belongs in the IaC
repo like a grant does; an operator at a laptop without the repo wired up
still needs a way through.

**5. Existing `local_iam` installs at 0.7.** (a) hard cut: refuse to start
until `connect` has run, one startup line, one changelog step; (b) one
release of grace with a banner, removal at 0.8. *Recommendation: (a).* The
rule you set is "never", the fix is one command, and a grace release means a
release in which the rule is a warning.

**6. Same-account installs.** Control and target the same account, separated
by identity: supported, and the quickstart's default. *Recommendation: yes.*
The alternative (require two accounts) rules out most first installs.

**7. Per-captain narrowing.** (a) an inline session policy, with wildcard
verbs to stay under 2,048 characters, and a managed-policy ARN only when a
declaration is too large; (b) a managed policy per captain always, passed as
`PolicyArns`; (c) a role per captain. *Recommendation: (a).* (b) makes every
captain install an IAM change; (c) also a trust-policy change each.

**8. One act role or one per captain.** (a) one act role whose policy is the
union of armed grants, each session narrowed to one action; (b) a role per
captain. *Recommendation: (a).* Same reasons as 7; the per-call narrowing
gives the same least privilege, and CloudTrail attributes by session name
and `sourceIdentity`, not by role.

**9. The grant's verification cadence.** Every 5 minutes while a grant is in
flight and on demand, versus only on demand. *Recommendation: the tick.* A
merged PR should arm an action without someone remembering to click.

**10. Where `permissions.github` and `egress` bite.** Informative only in
this spec. Enforcing GitHub scopes means a per-captain GitHub App
installation token with restricted permissions; enforcing egress means the
worker of 3.7. *Recommendation: declare now, enforce with 3.7.*

## Appendix A. What each first-party captain calls today

From `grep` over the captains at b34f14c (`.client("<service>")` and
`<verb>_<noun>(` pairs); the validator of section 8 turns this into a check.

**mapper** (`captains/mapper/aws_mapper.py`, `tf_state_graph.py`):
`sts:GetCallerIdentity`; `ec2:DescribeRegions`, `DescribeInstances`,
`DescribeSecurityGroups`, `DescribeNetworkInterfaces`; `ecs:ListClusters`,
`DescribeClusters`, `ListServices`, `DescribeServices`,
`DescribeTaskDefinition`; `lambda:ListFunctions`, `ListEventSourceMappings`;
`iam:ListRoles`, `ListRolePolicies`, `ListAttachedRolePolicies`,
`GetRolePolicy`, `GetPolicy`, `GetPolicyVersion`; `s3:ListAllMyBuckets`,
`GetObject` (state); `rds:DescribeDBInstances`; `dynamodb:ListTables`,
`DescribeTable`; `sqs:ListQueues`, `GetQueueAttributes`; `sns:ListTopics`,
`ListSubscriptionsByTopic`; `events:ListRules`, `ListTargetsByRule`;
`states:ListStateMachines`; `logs:DescribeLogGroups`;
`cloudwatch:DescribeAlarms`; `ecr:DescribeRepositories`;
`elasticloadbalancing:DescribeLoadBalancers`, `DescribeTargetGroups`;
`secretsmanager:ListSecrets`.

**honeypot** (`arm.py`, `investigate.py`, `correlation.py`): `sts:GetCallerIdentity`;
`iam:GetUser`, `ListAccessKeys`, `ListUserTags`, `GetPolicy`,
`GetPolicyVersion`, `GetRolePolicy`, `ListRolePolicies`,
`ListAttachedRolePolicies`; `events:DescribeRule`, `ListTargetsByRule`;
`cloudtrail:DescribeTrails`, `GetTrailStatus`, `LookupEvents`;
`tag:GetResources`; `ecs:ListClusters`, `DescribeServices`, `ListTasks`,
`DescribeTasks`, `DescribeTaskDefinition`; `ec2:DescribeNetworkInterfaces`;
`s3:ListAllMyBuckets`; `secretsmanager:ListSecrets`;
`rds:DescribeDBInstances`; `dynamodb:ListTables`. Actions: `iam:UpdateAccessKey` (scoped to the
decoy users' path), `ecs:StopTask`/`DescribeTasks`/`ListTasks` (scoped to
the whitelisted services' clusters), as `iam_deactivate_key.py` and
`ecs_restart_task.py` declare today.

**iac** (`import_job.py:105` → terraform plan, `-lock=false`): the mapper's
read set for the imported resource types, `s3:GetObject`/`ListBucket` on the
state bucket, `sts:GetCallerIdentity`. No DynamoDB lock table access.

**prs, librarian**: no AWS calls.

**Compass itself, control side** (`compass_core/commands.py`,
`captains/honeypot/alert_consumer.py`, `compass_core/secrets.py`,
`compass_api/auth/tokens.py`, `compass_api/routes/notifications.py`,
`captains/honeypot/alert_dispatcher.py`):
DynamoDB (local), `sqs:ReceiveMessage`/`DeleteMessage`/`GetQueueAttributes`,
`secretsmanager:GetSecretValue`, `ses:SendEmail`, `sts:GetCallerIdentity`.

## Implementation notes (2026-10-20)

Where the code differs from the sections above, and why. Each is small; none
changes the model.

- **Grant keys** are `GRANT#<captain>#<id>` (section 9.1 had the action type
  in the key). The decoy routes look a row up by its id, and a key that needs
  the type would turn every lookup into a query. The type is a field.
- **Verification without IAM's simulation.** Where `SimulatePrincipalPolicy`
  is unavailable (moto, so the e2e suite), a grant whose statements IAM holds
  is marked `verified` with `verification.simulated: false`, and the page says
  "statements present; IAM's simulation was not available". Real AWS always
  simulates; the live checklist covers it.
- **The access module's root file** is `access.tf`, next to the decoy
  captain's `services.tf`, rather than a block inside it: the core does not
  edit a captain's file.
- **Arming checks the queue policy** from the control side instead of
  sending a test event through the rule (section 14): a read role cannot
  `events:PutEvents`, and the policy is what goes wrong.
- **`honeypot replant_alerts`** (underscore, like every other command).
- **The inbox (4.7)** is its own spec; findings show on Settings → Account →
  Access and in `compass status` only.
- **`cfn-lint`** was not added as a dependency; `connect template` is checked
  for structure in the unit tests, and `aws cloudformation validate-template`
  is on the live checklist.
- **`connect` on an existing responder role** keeps the role `assume_role_respond`
  named (the migration maps it to `roles.act`); a fresh install's act role
  carries the installation suffix like the read role.
- **The static manifest-versus-code pass** skips a captain's `actions/` and
  `listener/` directories (the write actions are checked against their own
  statements; the listener runs in the target account with its own role).
- **Read-role growth after setup** (section 5) is not automatic yet: a captain
  install does not open a pull request on its own, and the Captains page does
  not say "waiting for the read policy". Re-running `connect`, or the next
  access pull request, regenerates the read policy, and the self-audit reports
  the gap (`missing`) until then. The install hook is the next small change.
- **`connect` does not open the import pull request itself** (section 9.2);
  the self-audit's "not under code" finding and the Access page's "Open the
  access PR" button do, with `imports.tf` on the first pull request.
