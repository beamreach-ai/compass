# Host seams: who is asking, who may, who must approve, where to tell them

Status: draft 2026-10-30, for review. Roadmap item 3.10 and the host side of
angle 3; part 1 of two, the product spec built on these seams stays with the
product. Line references are to `main` at 3897daf (0.9.0).

## 1. Goals and non-goals

**Goals.**

- A host (`compass_core.hosts`) can say who is calling, what they may do, who
  must approve an action, and how to reach people, the way it already says
  where jobs run, which model answers and where a workspace's config comes
  from: a registry with an open default and one `set_*` call in its install
  function.
- Nothing changes for the open edition. With no host installed, every route,
  every gate decision, every alert and every token behaves as in 0.9.0. That
  is the acceptance test for every pull request in §8.
- Captains declare their actions in the manifest, the same way they declare
  their admin prefixes and their permissions today, so a host's policy can
  name an action a captain added without knowing the captain.

**Non-goals.**

- No user database, team model, SSO, SCIM, approval matrix or signed ledger in
  the open core. Those are a host's; the product spec that builds on these
  seams is kept with the product.
- No new permission for the open single-admin install: with
  `APP_USERNAME`/`APP_PASSWORD` set, the one signed-in person remains admin.
- No change to API token scopes as users see them: `read`, `operate` and
  `admin` stay the token vocabulary, and the token's 403 keeps its shape.
- Collectors are specified here only as far as the seams need them (§3.5);
  `targets` ships in full with PR 6 (decision Q1), the audit record as far as
  the open edition stores it (§3.6, Q4).

## 2. Current state

**What a host can already replace.** `docs/architecture.md:166-182` lists the
seams: `queue.set_job_queue`, `config.set_workspace_config_loader` and
`set_workspace_config_saver`, `llm.set_model_factory`,
`sessions.set_session_factory` (`compass_core/sessions.py:127`),
`events.set_event_sink` (`events.py:57`), `notify.set_email_relay`
(`notify.py:39`), `licence.set_licence_resolver`, `inbox.register_source`, and
in the API `compass_api.extensions`: routers, startup and shutdown, bootstrap
and status sections, public paths, admin prefixes, identity headers
(`extensions.py:106`) and tabs. A host is a function the `compass.hosts`
entry point (or `COMPASS_HOSTS`) names; `tests/compass_core/test_core_hosts.py:44-93`
installs a fake one.

**Who is calling.** The middlewares, outermost first (`compass_api/app.py:166-177`):

1. `BearerAuthMiddleware` (`compass_api/middleware/bearer_auth.py:15-47`)
   resolves `Authorization: Bearer cmp_...` into an `ApiPrincipal`
   (`compass_api/auth/principal.py:22`: token id, kind, scope, workspace,
   org, user, name), **enforces the token scope itself** with
   `required_scope(method, path)` (`compass_api/auth/scopes.py:55`), and
   answers a 403 with `detail`, `required_scope` and `token_scope`
   (`bearer_auth.py:34-43`).
2. `WorkspaceContextMiddleware` (`compass_api/middleware/workspace.py:25-93`)
   takes the user id from the token, else from an identity header the host
   registered, else `X-User-Id` (`workspace.py:14-22`), and the workspace id
   from the token, else the header or query, else the default. When the host
   says a user's own id is their workspace (`compass_api/workspace.py:65`),
   **the workspace id is derived from the identity** (`workspace.py:63-65`).
   `/api/auth/me` and the health paths skip this middleware entirely
   (`workspace.py:44-47`). The cookie plays no part here: a cookie session's
   `user_id` is whatever the headers said, usually None.
3. `SimpleAuthMiddleware` checks the `compass_session` cookie when
   `APP_USERNAME` is set and answers 401 under `/api/`. A cookie session is
   not scoped: it may do anything.
4. `CaptainGateMiddleware` and `DemoAdminMiddleware`, which read the
   workspace config.
5. `SessionContextMiddleware` (`compass_api/middleware/sessions.py:1-5`),
   innermost, puts the user on the request as the requester of every AWS
   session the request opens.

Routes read the result through `get_actor_ids(request)`
(`compass_api/routes/_workspace.py:76-82`): `workspace_id`, `org_id`,
`user_id`, nothing else. `GET /api/auth/me` (`compass_api/routes/auth.py:77-94`)
returns the token's public form, or `{"authenticated": true, "auth_enabled":
false}`, or `username` for a cookie session; the TypeScript CLI probes it
(`cli/src/cli/local.ts:60`, `cli/src/tools/status.ts:83-92`) and so does the
login page (`dashboard/app/login/page.tsx:16`).

**What they may do.** Only tokens are checked, by URL prefix: `GET` is `read`,
writes are `operate`, and writes under the admin prefixes (`scopes.py:21-34`,
plus `extensions.admin_write_prefixes()` and each manifest's
`admin_write_prefixes`, `compass_core/captains.py:104`) are `admin`. The inbox
tells the dashboard which scope each button needs (`Action.scope`,
`compass_core/inbox.py:49-58`) and nothing filters the buttons by who is
looking.

**Who must approve.** `policy.gate(mode, risk, risk_max)`
(`compass_core/policy.py:264-276`) decides `run`, `wait_for_approval` or
`recommend_only`; the risk comes from `compute_risk_score` (`policy.py:211`)
and the model never sets it (`docs/specs/access-model.md:716-722`, rule 3.8).
Two callers: the decoy responder (`captains/honeypot/respond.py:117-121`),
which parks a `wait_for_approval` action in the incident's `pending` list and
runs it from `POST /api/honeypots/incidents/{id}/approve`
(`compass_api/routes/honeypot_incidents.py:147-162`, `respond.py:180`) or
drops it from `/decline` (`:165-181`, `respond.py:250`); and the PR watcher
(`captains/prs/policy.py:112-129`, `captains/prs/captain.py:172-177`), which
stores a `pending_revision` on the registry row. In both, any signed-in
caller's click is the approval, recorded as `approved_by`. Grants are accepted
by whoever calls `POST /api/access/grants/{id}/accept`
(`compass_api/routes/access.py:116-123`, `compass_core/grants.py:239`); there
is no gate on the acceptance itself, only on the actions it arms. The patching
captain's policy (`captains/patch/policy.py:43-62`, spec §3.4) returns `pr` or
`inbox` from KEV, EPSS, CVSS and exposure; it has no mode, risk or risk cap.

**How to reach people.** Notification endpoints are rows in the items table
with a `type`; the routes are create, list, delete and test
(`compass_api/routes/notifications.py:194-331`) plus the SES config. Delivery
is two `if/elif` chains over the type in the alert dispatcher: `_send`
(`captains/honeypot/alert_dispatcher.py:504-608`: `beamreach_email`, `smtp`,
`webhook`, `pagerduty`, `slack`, `email`) for alerts, and the incident-update
loop (`alert_dispatcher.py:400-445`) that posts to Slack, threaded when the
bot token allows, and mails the three email types. There are no routing
rules: an alert goes to every endpoint of the workspace, an update to the
endpoints that got the alert (`:400-409`). The admin prefix
`/api/notifications/routing` in `scopes.py:31` has no route behind it.

**The audit trail.** `events.emit_job_event` goes to the installed sink, which
appends to the running job; outside a job context events are dropped
(`compass_core/events.py:1-10`). Grants keep a `history` of transitions with
`by` (`grants.py:140-142`); the responder writes an audit row per action
(`captains/honeypot/responder.py`); the registry keeps the watcher's
decisions. API-side facts (a settings write, a token created, a click that
approved) are not recorded anywhere a host can sign.

## 3. The design

Each seam is a module with a registry, an open default that keeps today's
behaviour exactly, and a `set_*` a host calls once. Routes and captains call
the module, never the default directly.

### 3.1 Principal

`compass_api/auth/principal.py` grows one dataclass beside `ApiPrincipal`:

```python
@dataclass(frozen=True)
class Principal:
    id: str                       # stable: a user id, a token id, "system"
    kind: str                     # "user" | "token" | "system"
    workspace_id: str
    org_id: Optional[str] = None
    email: Optional[str] = None
    name: Optional[str] = None
    roles: Tuple[str, ...] = ()   # the host's vocabulary; the open default uses "admin" | "operate" | "read"
    teams: Tuple[str, ...] = ()   # ids in the host's directory; empty in the open edition
    source: str = "session"       # "session" | "api_token" | "header" | "anonymous" | the host's own
    attributes: Mapping[str, Any] = field(default_factory=dict)   # anything else the host wants routes to see
```

`ApiPrincipal` stays (tokens are an open feature) and maps to a `Principal`
with `kind="token"`, `roles=(scope,)`, `source="api_token"`.

**The resolver seam.**

```python
Resolver = Callable[[Request, Optional[str], Optional[str]], Optional[Principal]]
# (request, user_id_from_headers, requested_workspace_id) -> Principal, or None for "not signed in"

def set_principal_resolver(fn: Optional[Resolver]) -> None: ...
def resolve_principal(request, user_id, requested_workspace_id) -> Optional[Principal]: ...
```

The resolver runs **before** the workspace is settled, because for the host's
personal workspaces the workspace is derived from the identity
(`workspace.py:63-65`). `WorkspaceContextMiddleware.dispatch` calls it once
with the header identity and the requested workspace id, stores the result as
`request.state.principal`, takes `principal.workspace_id` as the workspace
(the resolver applies the user-workspace rule; the open default reproduces
`uses_user_workspace` and the default-workspace fallback), and loads the
context with `user_id=principal.id`, so nothing downstream changes.

**Open default**, in this order:

1. `request.state.api_principal` set: from the token, workspace from the
   token (a conflicting header is still a 403, as today).
2. An identity header (`extensions.identity_headers()`, then `X-User-Id`):
   `Principal(id=<header>, kind="user", roles=("admin",), source="header")`.
   This is today's behaviour: the header is trusted because a proxy set it,
   and it is today's `user_id`, so it comes before the cookie.
3. A valid `compass_session` cookie: `Principal(id=APP_USERNAME, kind="user",
   roles=("admin",), source="session")`. New information: today a cookie
   session's `user_id` is None, so `approved_by` and `created_by` say
   `"user"`; after, they say the username.
4. Nobody (auth off, or no valid cookie, which `SimpleAuthMiddleware` still
   turns into a 401 under `/api/`): `Principal(id="anonymous", kind="user",
   roles=("admin",), source="anonymous")`, whose `user_id` is None, so
   nothing that reads `ctx.user_id` changes.

In each case `workspace_id` is the requested one, or the user's own when
`uses_user_workspace`, or the middleware's default.

**Where it surfaces.**

- `get_actor_ids(request)` adds `principal_id` and `principal` (the public
  form). Captains spread it into job payloads (`{**payload, **actor}`), so
  flat keys like `name` or `email` would shadow a payload's own; two keys
  that cannot collide are all it adds. Every `actor=` already passed to
  grants, the responder and the watcher keeps working; `user_id` stays
  `principal.user_id`.
- `GET /api/auth/me` keeps every key it returns today (`authenticated`,
  `auth_enabled`, `method`, `username`, the token's public form) and adds
  `principal: {id, kind, email, name, roles, teams, source, workspace_id}`.
  The route stays outside the workspace middleware and calls
  `resolve_principal` itself with the requested workspace id, so the TypeScript
  CLI's probes and the login page keep working unchanged.
- `compass whoami` in both CLIs (the Python `compass`, whose command table
  `tests/compass_core/test_core_cli.py:65-67` pins, and the TypeScript CLI
  under `cli/`), and an MCP tool `whoami` in `cli/src/mcp/server.ts`.
- Job payloads carry `principal_id` beside `user_id`. `sessions.target`
  (`sessions.py:236-272`) keeps using the requester as the `SourceIdentity`.

### 3.2 Authoriser

**Actions and resources.** Routes stop reasoning in URL prefixes and ask one
question:

```python
# compass_api/auth/authz.py
@dataclass(frozen=True)
class Resource:
    workspace_id: str
    captain: Optional[str] = None
    account_id: Optional[str] = None      # a target account
    environment: Optional[str] = None     # the host's label (prod, staging); None in the open edition
    kind: Optional[str] = None            # "grant" | "pr" | "decoy" | "incident" | "settings" | "job" | ...
    id: Optional[str] = None

Decision = Literal["allow", "deny"]
Authorizer = Callable[[Principal, str, Resource], Decision]

def set_authorizer(fn: Optional[Authorizer]) -> None: ...
def authorize(principal, action, resource) -> Decision: ...
def require(request, action, resource=None) -> Principal:   # raises 403
def action_for(method, path) -> str:                         # the generic rule, from required_scope
```

Action names are dotted strings. The core fixes this vocabulary and captains
extend it in their manifests:

| Action | Today's scope |
|---|---|
| `*.read` (any GET) | read |
| `job.run`, `decoy.plant`, `iac.import`, `pr.revise`, `inbox.snooze` | operate |
| `approval.approve`, `approval.decline` | operate (the policy may ask for more, §3.3) |
| `grant.accept`, `grant.decline`, `grant.remove`, `grant.widen` | admin (`/api/access` is an admin prefix) |
| `settings.write`, `token.manage`, `captain.install`, `captain.enable` | admin |

A manifest declares `actions={"sre.survey": "operate", "sre.act": "admin"}`
beside `admin_write_prefixes`; `compass validate` checks the names are dotted
and lower case and the scopes are token scopes.

**Open default.** `required_scope(method, path)` is kept as the mapping from
route to scope and becomes the open authoriser's input: allow when the
principal's roles cover the scope the route needs (`scope_covers`). Cookie,
header and anonymous principals have `roles=("admin",)`, so they pass
everything, which is today. Tokens behave exactly as today.

**Where it runs.**

- The token scope check **moves out of** `BearerAuthMiddleware`, which keeps
  only authentication, into `WorkspaceContextMiddleware`: once it has the
  principal it calls `authorize(principal, action_for(method, path),
  Resource(workspace_id))`. A deny is a 403 whose body keeps today's
  `detail`, `required_scope` and `token_scope` (the CLI reads them) and adds
  `action`, so the dashboard can say why. One check per request, not two.
- Routes that act on a specific thing call `require(request, "grant.accept",
  Resource(kind="grant", id=grant_id, account_id=...))` so a host can decide
  per account or environment. First-party routes to convert:
  `routes/access.py`, `routes/honeypot_incidents.py` (approve, decline,
  respond), `routes/honeypot_actions.py`, `routes/prs.py` (watcher settings,
  revise), `routes/settings.py`, `routes/tokens.py`, `routes/captains.py`,
  `routes/inbox.py` (snooze), `routes/patches.py`.
- The inbox: `Action` gains `action` (the dotted name) beside `scope`, and
  `GET /api/inbox` (`routes/inbox.py:60`) drops the actions the principal may
  not take, so a viewer sees the item but not the button. The dashboard needs
  no change for that.

### 3.3 Approval policy

**What changes.** `policy.gate` stays as a function and becomes the open
default *decision*. New: a request object, a decision that can name
approvers, a record of who approved, and one place every caller goes through.

```python
# compass_core/approvals.py
@dataclass(frozen=True)
class ApprovalRequest:
    id: str                      # deterministic per subject (D2): "honeypot:<incident_id>:<action_id>", "prs:<repo>#<number>:<revision>", "grant:<grant_id>"
    workspace_id: str
    captain: str
    action: str                  # "decoy.respond", "pr.revise", "grant.accept", "sre.act"
    risk: int                    # 1-5, from compute_risk_score; never a model output
    subject: Mapping[str, Any]   # what it is about: incident, PR, grant, resource ids
    resource: Resource           # account, environment, kind, id (the authoriser's type)
    requested_by: Optional[str]  # principal id, or None when a schedule asked

@dataclass(frozen=True)
class Requirement:
    count: int                   # approvals needed
    from_roles: Tuple[str, ...] = ()
    from_teams: Tuple[str, ...] = ()
    not_requester: bool = False  # two-person rule

@dataclass(frozen=True)
class ApprovalDecision:
    decision: str                # "run" | "wait_for_approval" | "recommend_only"
    reason: str
    requirements: Tuple[Requirement, ...] = ()   # empty unless wait_for_approval
    expires_at: Optional[datetime] = None

class ApprovalPolicy(Protocol):
    def decide(self, req: ApprovalRequest, *, mode: str, risk_max: int) -> ApprovalDecision: ...

def set_approval_policy(policy: Optional[ApprovalPolicy]) -> None: ...
def decide(req, *, mode, risk_max) -> ApprovalDecision: ...
def approve(req_id, principal, *, workspace_id, note=None) -> ApprovalState: ...
def decline(req_id, principal, *, workspace_id, note=None) -> ApprovalState: ...
def state(req_id, *, workspace_id) -> ApprovalState:   # pending | satisfied | declined | expired, with the approvals so far
```

**Open default policy:** `decide` wraps `gate(mode, risk, risk_max)`; when
the answer is `wait_for_approval` the requirement is one approval from role
`operate`, `not_requester=False`. That is today exactly, and the five
assertions in `tests/captains/honeypot/test_actions.py:205-209` hold through
the wrapper.

**Storage (Q2).** A table of its own, `approvals` in `dynamodb_config`
beside `items` and `jobs` (`compass_core/tables.py:58-61`), named
`compass_approvals_<workspace>` by the same rule. `APPROVAL#<id>` rows carry
the request, decision, requirements, `approvals: [{principal_id, at, note}]`
and status, with a GSI on `status` then `requested_at` so "what waits" is one
query; from PR 5 the same table holds the audit rows (§3.6). `init_tables`
creates it, a migration adds it to an existing install, and the Terraform
module, `docker-compose.yml` and the e2e world (`e2e/conftest.py`) learn it.
In memory when there is no table, like `compass_core/registry.py:6,25`. The
row is the inbox item's source of truth for kind `approval.needs_you`; the
captain's own pending state (the incident's `pending` entry, the registry's
`pending_revision`) keeps the request id and nothing else new.

**Callers.** Every place that calls `gate` today calls `approvals.decide`
instead, and every "approve" route calls `approvals.approve`, which first runs
the authoriser (`approval.approve` on the request's resource), then checks the
requirements against the approver's roles and teams, then records. The action
runs only when `state(req_id).status == "satisfied"`.

| Caller | Today | After |
|---|---|---|
| decoy responder (`respond.py:117-121`) | `gate`; `pending` on the incident; `/incidents/{id}/approve` | `decide`; the pending entry carries the request id; the route calls `approvals.approve` and runs when satisfied |
| PR watcher (`prs/policy.py:128`, `prs/captain.py:172-177`) | `gate` → `pending_revision` | same shape; `pending_revision` carries the request id |
| grants (`grants.py:239`, `access.py:116-123`) | the caller's identity | `decide` with `action="grant.accept"`; the open default yields one operate approval, so accept-on-click is unchanged |
| patching captain (`patch/policy.py:43-62`) | `pr` or `inbox` from the scores | its policy still decides; the outcome becomes an `ApprovalRequest` (below) |
| future captains | — | `decide` |

**The patching captain (Q3).** Its policy keeps deciding `pr` or `inbox`
from KEV, EPSS, CVSS and exposure. The captain then builds an
`ApprovalRequest` with `action="patch.pr"`, the finding's resource, and a
risk derived from the scores: 5 when exploited in the wild or the workload
faces the internet, 4 when CVSS is 9 or above, else 3. It calls `decide`
with `mode="autopilot", risk_max=5` when its policy said `pr` and
`mode="approve"` when it said `inbox`, so the open default reproduces today's
two outcomes exactly (`pr` runs, `inbox` waits for one operate approval) and
a host's policy sees a risk and a resource it can reason about. The inbox
item for a waiting fix becomes an `approval.needs_you` item; the patching
spec's §3.4 is amended in the same PR.

The LLM never sets `risk`, `mode` or `risk_max`; access-model rule 3.8 becomes
a test here: no `ApprovalRequest.risk` is constructed from model output.

**Notify on wait.** When `decide` returns `wait_for_approval`, `approvals`
sends a `Message` with `actions` (§3.4) through `channels.send` to the
workspace's endpoints, the same audience an alert has today. A host can
resolve the requirement's teams to channels by installing its own audience
function (`set_approval_audience`); the open default is the endpoint list.

### 3.4 Channels

**Registry.**

```python
# compass_core/channels.py
@dataclass
class Message:
    subject: str
    text: str
    html: Optional[str] = None
    severity: str = "high"
    blocks: Optional[List[Dict[str, Any]]] = None     # Slack-shaped rich content; a channel may ignore it
    actions: Tuple[MessageAction, ...] = ()           # buttons: label, action ("approval.approve"), request_id
    thread: Optional[Mapping[str, Any]] = None        # continue an earlier delivery

Sender = Callable[[Dict[str, Any], Message, "Context"], Optional[Dict[str, Any]]]
# (endpoint record, message, context with workspace config and relay credentials) -> delivery info

def register_channel(type: str, sender: Sender, *, schema: Dict[str, Any], label: str, supports_actions: bool = False, supports_threads: bool = False) -> None
def send(endpoint, message, ctx) -> Optional[Dict[str, Any]]
def channel_types() -> List[Dict[str, Any]]       # type, label, schema, supports_actions, supports_threads
```

Both dispatch sites move: `_send` (`alert_dispatcher.py:504-608`) becomes six
`register_channel` calls at import (`webhook`, `slack`, `pagerduty`, `smtp`,
`email`, `beamreach_email`) and one `channels.send`; the incident-update loop
(`:400-445`) becomes `channels.send` with `Message.thread` set, and the Slack
sender keeps returning `{"slack_thread": ...}` so threading is unchanged.
`beamreach_email` keeps going through `notify.email_relay()`.

**What a host or captain adds.**

- A new type (Teams, Opsgenie, a ticket system) is one `register_channel`
  call. `GET /api/notifications/channel-types` lists the registry so the
  settings page offers it and draws its form from `schema` (the same
  JSON-Schema-to-form the captain settings already use).
- Interactive replies: a channel with `supports_actions=True` registers its
  own callback router (`extensions.register_router`) and, on a button press,
  calls `approvals.approve`/`decline` with the principal its platform
  authenticated. The core does not standardise the callback; it standardises
  what the button carries (`MessageAction.action`, `request_id`).
- Routing rules stay out of the open core (decision Q7). The audience
  of an alert, an update and an approval request is the workspace's
  endpoints, as today.

### 3.5 Collectors and targets

Needed by SRE adapters, Kubernetes and a second cloud. The collector
registry and `NodeKind` belong to 5.8; `targets` is the 1.8/11.3 work and
ships whole in PR 6 (decision Q1). This section fixes the shapes the other
seams read; the onboarding flow, the StackSet module and the per-target
`compass connect` are described in PR 6's own text when it opens.

- `captains/mapper/collectors.py`: `register(name, fn, *, scope="region" |
  "account", provider="aws", requires=())`; `_collect_region`
  (`captains/mapper/aws_mapper.py:470-491`) iterates the registry in
  dependency order. The seventeen first-party collectors register themselves.
  A captain registers more from its manifest
  (`collectors=("beamreach.captains.sre.collectors",)`), and its declared
  reads (`permissions.aws.read`, `captains.py:116`) must cover them.
- Node and edge types are open strings already; a collector that adds a new
  type also registers a `NodeKind` (label, icon, parent kind) so the map can
  draw it.
- `targets`: a list in the workspace config, each `{id, provider,
  account_id, regions, role_arn, labels: {environment, ou, ...}}`.
  `sessions.target(capability, target=...)` picks the role; the mapper runs
  per target; every node carries `target_id`. The `labels` are what the
  authoriser's `Resource.environment` and the approval policy read. The open
  default is one target, built from today's `aws` config, so nothing moves.

### 3.6 Audit record

The job event sink cannot carry this: it needs a running job
(`events.py:1-10`), and a deny, an approval, a settings write or a token
creation happens in an API request with none. So:

```python
# compass_core/audit.py
def record(event: Mapping[str, Any]) -> None
def set_audit_sink(sink: Optional[Callable[[Mapping[str, Any]], None]]) -> None
```

with one event shape a host's sink can sign:

```
{kind: "audit", at, workspace_id, principal_id, action, resource: {...},
 decision: "allow"|"deny"|"run"|"wait_for_approval"|"recommend_only",
 approval_id?, approvals?: [...], pr?: {repo, number, sha}, model?: {provider, id},
 job_id?, request_id?, note?}
```

`request_id` is the id the session middleware mints for the request
(`middleware/sessions.py:1-5`), so an audit event and the AWS sessions it led
to share a name. Emitted by: `authorize` on deny,
`approvals.decide/approve/decline`, grant transitions, PR open and merge (the
registry), settings writes, token creation.

**Open default (Q4).** One log line always, and an `AUDIT#<at>#<id>` row in
the approvals table (§3.3) with a TTL of `audit.retention_days` (default 90).
`GET /api/audit?since=&action=&principal=` lists them (an admin read), the
Settings page shows the last hundred, and `compass audit list` prints them.
When a job is running the event is also appended to it through the existing
sink, so a job's page still tells the whole story. A host's sink replaces
the row, not the log line.

## 4. Configuration

New in the open workspace config: `targets` (§3.5), defaulting to one entry
built from `aws`; `dynamodb_config.approvals`, the new table's name, which
`init_tables` and the migration fill in; and `audit.retention_days` (90). A
host configures its resolver, authoriser, policy, channels and audit sink in
code, in its install function. No environment variable is added.

## 5. What changes where

| Area | Change |
|---|---|
| `compass_api/auth/principal.py` | `Principal`, `Resolver`, `set_principal_resolver`, `resolve_principal`, `ApiPrincipal.to_principal()` |
| `compass_api/auth/authz.py` (new) | `Resource`, `authorize`, `require`, `action_for`, `set_authorizer` |
| `compass_api/auth/scopes.py` | unchanged API; `required_scope` feeds the default authoriser; the dead `/api/notifications/routing` prefix goes |
| `compass_api/middleware/bearer_auth.py` | authenticates only; the scope check leaves |
| `compass_api/middleware/workspace.py` | resolves the principal first, derives the workspace from it, runs the generic authorisation |
| `compass_api/routes/_workspace.py` | `get_actor_ids` gains `principal_id` and `principal` |
| `compass_api/routes/auth.py` | `/api/auth/me` adds `principal`, keeps today's keys |
| first-party routes (§3.2) | `require(...)` on the specific thing; approve routes call `approvals.approve` |
| `compass_core/inbox.py`, `routes/inbox.py` | `Action.action`; the list filters by the authoriser |
| `compass_core/captains.py`, `compass validate` | manifest `actions`; names checked |
| `compass_core/approvals.py` (new) | request, decision, policy seam, storage, approve/decline/state, notify on wait |
| `compass_core/tables.py`, `init_tables`, `compass_core/migrations`, the Terraform module, `docker-compose.yml`, `e2e/conftest.py` | the approvals table |
| `captains/patch/policy.py`, `captains/patch/pr.py`, `captains/patch/inbox.py` | the outcome becomes an `ApprovalRequest`; the waiting fix is an approval item |
| `captains/honeypot/respond.py`, `captains/prs/policy.py`, `captains/prs/captain.py`, `compass_core/grants.py` | call `approvals.decide`; pending state carries the request id |
| `compass_core/channels.py` (new), `captains/honeypot/alert_dispatcher.py` | registry; both dispatch sites become `channels.send` |
| `compass_api/routes/notifications.py` | `GET /api/notifications/channel-types`; endpoint form from the schema |
| `compass_core/audit.py` (new) and the emitters | `record`, `set_audit_sink`, the event shape, the rows with a TTL, `GET /api/audit` |
| `captains/mapper/collectors.py` (new), `aws_mapper.py`, `sessions.py`, workspace config | registry, `targets`, `NodeKind` |
| Python CLI (`compass_core/cli.py:26-36`), TypeScript CLI and MCP (`cli/src/tools/access.ts`, `cli/src/mcp/server.ts`) | `whoami`; `approvals list\|approve\|decline`; `audit list`; `status` gains "approvals waiting on you"; **`access accept` and `access decline` are removed** (Q8): a grant is approved like anything else. `access list\|remove\|verify\|audit\|pr` stay. The API routes under `/api/access/grants/{id}` stay and call `approvals`, so the dashboard and the inbox buttons do not change. A breaking CLI change, in the changelog and the skill |
| dashboard | the shell reads `/api/auth/me` once (`useMe()`), shows `name \|\| id`, hides controls the inbox or a 403 said are denied; no new page |
| docs | `docs/architecture.md` hosts section lists the new seams; `docs/writing_a_captain.md` gains `actions`; `docs/inbox.md` the filtered buttons; the `compass-open` skill the new verbs |

## 6. Test strategy

- Every existing suite stays green **unchanged**: no test edits in the seam
  PRs except where a test asserts a private name that moved. That is how
  "nothing changes for the open edition" is proved.
- One test per seam in the style of `tests/compass_core/test_core_hosts.py`:
  a fake host installed through `COMPASS_HOSTS` sets the seam, and the
  behaviour changes; with the host removed, it is back.
- Property-style checks that pin the open default to today: for every
  `(method, path)` the route table knows, `action_for` and `required_scope`
  agree; for every `(mode, risk, risk_max)`, `approvals.decide` and `gate`
  agree; for every endpoint type, `channels.send` calls what `_send` called
  (the fakes in `tests/e2e` already log every request).
- The middleware order has a test of its own: a token on a personal-workspace
  host, a header identity on the open default, a cookie session, and auth
  off, each ending with the expected `request.state.principal` and
  workspace.
- The access-model rule as a test: no `ApprovalRequest` is built with a risk
  that came from a model answer (a grep-style test over the callers, like the
  one for `policy.gate` inputs).
- The new table under `moto`: `init_tables` creates it with the GSI and the
  TTL attribute, the migration adds it to a config that lacks it, and every
  approvals and audit test runs once with the table and once in memory.
- The patching mapping: for every finding fixture in `tests/captains/patch`,
  the old `decide` outcome and the new `approvals.decide` decision agree.

## 7. Risks

- **A second scope check.** If the token check stays in the bearer middleware
  while the authoriser runs too, a host's `allow` cannot override the token
  scope and the two 403 bodies differ. Mitigation: the check moves (§3.2) and
  the 403 keeps its keys.
- **Personal workspaces.** Resolving the principal after the workspace would
  break the host's user-workspace rule silently. Mitigation: the order in
  §3.1 and the middleware-order test.
- **Dropped audit events.** Routing audit through the job sink would lose
  every API-side event. Mitigation: `audit.record` is its own seam (§3.6).
- **Two CLIs drift.** A verb added to one CLI and not the other. Mitigation:
  `test_core_cli.py` pins the Python table; the TypeScript tests pin the MCP
  tool list; the PR checklist names both.
- **The deterministic id collides.** One incident parks several actions.
  Mitigation: the id includes the action id (§3.3).
- **A new table is a migration for every install.** The compose updater,
  Terraform and the e2e update path all have to create it. Mitigation: the
  migration runs from `init_tables` and from `compass update apply`, and the
  in-memory fallback keeps an install working until it has run.
- **Removed CLI verbs break scripts.** `compass access accept` disappears.
  Mitigation: the changelog entry, the skill, and a one-release stub that
  prints the new command and exits non-zero.
- **Behaviour creep.** It is tempting to narrow cookie sessions while
  touching this. D4 says no; the acceptance test catches it.

## 8. PR plan

Each PR keeps the existing suites green unchanged and adds the fake-host test
for its seam.

| PR | Content | Proof |
|---|---|---|
| 1 | `Principal`, resolver seam, middleware order, `get_actor_ids`, `/api/auth/me`, `whoami` in both CLIs and MCP | a fake resolver sets `roles=("read",)` and `/api/auth/me` shows it; a fake user-workspace host gets its workspace from the identity; open suites unchanged |
| 2 | `authz`: actions, `Resource`, `authorize`, `require`, `action_for`; the scope check moves; first-party routes converted; inbox action filtering; manifest `actions` in `compass validate` | a fake authoriser denying `grant.accept` turns the route into a 403 and hides the inbox button; tokens get the same 403 body as before |
| 3 | `approvals`: request, decision, the approvals table, `approve`/`decline`/`state`; responder, watcher, grants and the patching captain migrated; notify on wait; `approvals` verbs in both CLIs and MCP, `access accept\|decline` removed; inbox kind | a fake policy requiring two approvals from team `sec` leaves a decoy response parked after one click and runs it after the second; the default policy reproduces every existing gate test and every patching outcome |
| 4 | `channels`: registry, the six built-ins and both dispatch sites moved, `channel-types` endpoint, settings form from schema, `Message.actions` | a fake `teams` channel registered by a host appears in the settings list and receives the approval message; the e2e fakes see the same requests as before |
| 5 | `audit`: `record`, `set_audit_sink`, the event shape, the emitters, the rows with a TTL, `GET /api/audit`, `audit list` | the fake sink receives one event per deny, decision, approval, grant transition, settings write and token creation; without a host the same events are rows with a TTL |
| 6 (is 1.8/11.3) | collectors registry; `NodeKind`; `targets` in full: the config shape, `compass connect` per target, the StackSet module and the onboarding page | a fake collector registered by a host adds nodes; a two-target config maps both and every node carries its `target_id`; the e2e world gains a second account |

PRs 1–3 unblock the product's identity and approvals; 4 unblocks Teams and
approval cards; 6 unblocks the SRE adapters and Kubernetes. Sizes, one
engineer: 1 under a week; 5 one week; 2 and 4 two weeks each; 3 three
weeks (the table and the patching mapping); 6 five weeks.

## 9. Decisions taken (2026-10-30, from the questions)

- **D1.** Roles are strings the host defines; the open default uses the
  three token scopes as role names so one vocabulary serves both.
- **D2.** The approval request id is deterministic per subject, so a page
  refresh, a Slack button and the CLI all act on the same request; for a
  decoy response the subject is the incident and the action.
- **D3.** The core standardises the button payload, not the callback
  transport: each interactive channel owns its webhook.
- **D4.** Cookie, header and anonymous sessions stay all-powerful in the open
  edition. Narrowing them is a host's job; the open single-admin install does
  not change.
- **D5.** The token scope check lives in the authoriser, not the bearer
  middleware, and the 403 body keeps `required_scope` and `token_scope`.
- **D6.** The principal is resolved before the workspace, and the principal
  names the workspace.
- **D7.** Audit events have their own seam; the job sink is not reused.
- **Q1.** `targets` ships in full in PR 6, which is the 1.8/11.3 work:
  the config shape, the single-target default, `compass connect` per target,
  the StackSet module and the onboarding page.
- **Q2.** Approvals get a table of their own, `dynamodb_config.approvals`,
  not rows on the items table.
- **Q3.** The patching captain migrates in PR 3 with the mapping in §3.3:
  `pr` runs, `inbox` waits, and the risk is 5 for KEV or an exposed workload,
  4 for CVSS 9 or above, else 3.
- **Q4.** Without a host sink, audit events are rows in the approvals table
  with a 90-day TTL, listed by `GET /api/audit`, plus the log line.
- **Q5.** `/api/auth/me` grows a `principal` key and keeps every key it has.
- **Q6.** The generic authorisation runs in `WorkspaceContextMiddleware`,
  so a captain's router is covered without opting in.
- **Q7.** Routing rules stay out of the open core; the audience of an alert,
  an update and an approval request is the workspace's endpoints, and the
  dead `/api/notifications/routing` prefix goes.
- **Q8.** `compass access accept` and `compass access decline` are removed
  from both CLIs in PR 3; `compass approvals approve|decline` is the one way.
  The API routes stay.

## Open decisions

None at 2026-10-30.

## Implementation notes (2026-10-30, PR 1)

- `Principal.workspace_id` is Optional: the resolver names the workspace when
  it knows it (a token, a personal workspace), and the middleware fills the
  default in; `request.state.principal` always carries the final one.
- `Principal.user_id` is the id, or None for `source="anonymous"`. The
  middleware passes that to the workspace loader, so `ctx.user_id` is what
  it was: nobody is still None, not `"anonymous"`.
- The identity header comes before the cookie (§3.1), because the header is
  today's `user_id`; the spec's original order would have changed it behind
  a proxy that also sets `APP_USERNAME`.
- `/api/auth/me` resolves the principal itself, outside the middleware, and
  keeps every key it had. `compass whoami` in the TypeScript CLI prints the
  `principal` key, and stands in for an older engine from the keys it finds.
- The Python `compass whoami` has no request to resolve: it answers for the
  operator of the install (`APP_USERNAME`, else the OS user), `admin`,
  `source="cli"`, in the same shape.
- `get_actor_ids` adds two keys, not six: the example captain spreads it
  into its job payload and a flat `name` overwrote the payload's `name`
  (found by `tests/test_writing_a_captain.py`).
- Job payloads do not carry `principal_id` yet; it equals `user_id` until a
  host resolves people, and PR 3's `requested_by` is where it is first read.
