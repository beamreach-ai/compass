# Access: what Compass may do in your account

Compass only reads. Its write access in a target account is exactly the set of
actions a person has enabled, and each of those is granted and revoked by a
pull request that a person merges and applies. This page is the model behind
`compass connect`, the two roles, the grants and the audit;
[docs/specs/access-model.md](specs/access-model.md) is the design with its
decisions.

## The two roles

`compass connect` creates two IAM roles in the target account, under the path
`/service/`, named for this installation (`aws.install_id`, six characters
`init_config` generates):

| Role | Policy | Who may assume it |
|---|---|---|
| `svc-inventory-reader-<install>` | generated: the union of what the installed captains declare they read, plus `sts:GetCallerIdentity` and the IAM reads the self-audit needs on these two roles only | the base identity, with the external id |
| `svc-ops-responder-<install>` | empty until a grant is merged; then the union of the enabled actions | the base identity, with the external id |

The names are cover names (the decoys' value drops if IAM announces the
deception tooling); `read_role=` and `act_role=` on `connect` choose others.
Both roles carry the tag `managed-by=compass` (`aws.session_prefix`).

**The read policy is generated**, never written by hand. Each captain's
manifest declares its reads (`permissions.aws.read`), with wildcards per
service; `compass validate` checks that the captain's code calls nothing the
block does not cover. `connect` renders the union into the role. After
installing a captain that reads more, run `connect` again while the roles are
not under code, or open the access pull request (Settings → Account → Access,
`compass access pr`) once they are: it regenerates the read policy too. The
self-audit reports the gap until then.

## The base identity

The containers need one AWS identity to call `sts:AssumeRole`:

- **On AWS** (ECS, EC2) it is the task role or instance profile. `connect`
  finds it through the instance metadata and trusts it; `base_identity=<arn>`
  overrides the detection. Nothing is stored.
- **Elsewhere** `connect` creates a runtime user, `svc-compass-runtime-<install>`,
  in the control account, with one inline policy: `sts:AssumeRole` on the two
  roles, `sts:GetCallerIdentity`, and receive/delete on the alert queues. Its
  access key goes into `config/local.json` under `aws.control.credentials`.
  `connect rotate` replaces it. If the file leaks, the holder can assume the
  read role and drain the alert queue, and run the armed actions; the key and
  the external id are in the same file. IAM Roles Anywhere is the hardened
  alternative for anyone who cannot accept a key on disk; it fits the same
  `aws.control` shape (`credential_source`).

Once `connect` has run, the services check at start that no `AWS_PROFILE` is
set, no `~/.aws` is mounted, and the credentials they run with are the runtime
identity `connect` recorded. Before that they start as they are, so an install
updated with its old compose file comes up, shows the Connect card, and can run
`connect` with the profile it already mounts; remove the mount afterwards. The
profile is for `connect` only.

## Every session, named

Every AWS session Compass makes comes from `compass_core.sessions`:

- `control()` is the base identity, for the control account's own resources
  (DynamoDB, the alert queues, Secrets Manager, SES) and for AssumeRole.
- `target("read")` assumes the read role, named
  `compass-<captain>-<job>` (`compass-mapper-<job id>` for a scan,
  `compass-honeypot-api-<request>` for work done from the dashboard), with an
  inline **session policy** that is the captain's own declaration. STS
  intersects it with the role's policy, so a captain cannot read more than it
  declared whatever the role allows.
- `target("act")` assumes the act role, never cached, one session per action,
  narrowed to that action's statements once the grant layer names it.
- Every session sets `SourceIdentity` to the person who asked (their email,
  from the dashboard session; `scheduler` for a scheduled job) once the trust
  policy allows it; `connect` writes both.

In the target account's CloudTrail, an event therefore shows
`userIdentity.arn: …:assumed-role/svc-inventory-reader-k3f9a2/compass-mapper-<job>`
and `sourceIdentity: ilia@example.com`: which captain, which job, who asked.

## Decoy alerts flow out

The queue decoy alerts report to lives in the control account (one per region
the decoys are in), with SQS-managed encryption. Its policy lets the target
account's own roles send (the decoy modules' `alert_delivery` roles and the
listener's task role, as the target's own IAM decides) and the base identity
receive. After `connect`, nothing Compass does writes into a target account
except through a granted action.

## Grants: writes by pull request

A response action (`iam_deactivate_key`, `ecs_restart_task`) is a **grant**:
a row with the action, its scope, the mode (`off`, `approve`, `autopilot`)
and a lifecycle:

```
proposed → pr_open → merged → applied → verified → armed
                                     disarmed → revoke_pr_open → revoke_merged → revoked
```

Accepting an action opens a pull request that regenerates the access module
(`services/_ops_access/` in your IaC repository: both roles, their trust, the
act role's policy as the union of the enabled grants). The pull request watcher
sees the merge; the grant is `applied` only when Compass reads the role back
from IAM and finds the statements, `verified` when `iam:SimulatePrincipalPolicy`
allows them, and `armed` from then on. A row that is not armed never reaches
`execute`, whatever its mode says. Removing an action disarms it at once and
opens the pull request that takes the permission away.

## The self-audit

Every 24 hours, and on demand, Compass reads both roles' trust policies,
inline and attached policies, computes what they should hold (the manifests,
the grants, the config) and reports the difference: a statement added in the
console, an attached managed policy, a second trusted principal, a grant that
says armed but is not in the role, a role that no connected repository holds.
Each finding has one action: the pull request that regenerates the module.

## Commands

| Command | What it does | Writes to AWS |
|---|---|---|
| `connect account=… region=…` | the roles, the queues, the runtime identity, the verification, the config | yes, once |
| `connect verify` | assume the roles as the base identity; simulate the read policy | no |
| `connect rotate` | a new key for the runtime user, the old one deleted | yes |
| `connect template account=… base_identity=…` | the roles as CloudFormation and Terraform, for an account out of reach | no |

`connect` arguments: `regions=` (comma-separated), `control_account=`,
`base_identity=<arn>` or `none`, `read_role=`, `act_role=`, `runtime_user=`,
`state_bucket=` (the Terraform state the mapper may read).

## The canary's grant

The patch captain's one action, `patch.canary_deploy`, runs a fix's image on a
staging twin and rolls it back ([patching.md](patching.md)). It goes through
this flow like a decoy response: proposed by the captain when `patch.canaries`
names a twin, accepted by a person, applied by the access pull request,
verified against IAM, armed. Its statements name the exact staging services
and clusters and the task roles they run as; nothing in them reaches the
service that serves traffic.
