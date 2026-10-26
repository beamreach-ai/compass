# The inbox

Everything that needs a person, as one list: `GET /api/inbox`, newest first,
with a count per tab and the routes that act on each item. Nothing in it is
stored. Every item is derived from state the captains already keep, so it
appears when the state says a person is needed and leaves when the state
changes; nothing is marked read and nothing can go out of sync.

## The kinds

| Kind | Where it comes from | Verb · severity | Actions | Leaves when |
|---|---|---|---|---|
| `decoy.approval` | a response the gate parked on an open incident (mode `approve`, or above the risk cap) | approve · urgent (risk ≥ 3), else normal | Run, Decline, Open incident | run, declined, or the incident closed |
| `grant.proposed` | a grant in `proposed` | approve · normal | Accept, Decline, Open access | accepted or declined |
| `grant.waiting` | a grant whose access pull request is open | merge · info | Open pull request, Verify now | merged |
| `decoy.waiting` | a decoy whose plant or decommission pull request is open | merge · info | Open pull request, Open decoy | merged |
| `decoy.arm` | a decoy whose plant pull request merged and that is not armed | fix · normal | Arm, Open decoy | armed |
| `pr.revision` | a tracked pull request with a revision waiting for approval | approve · normal | Approve the revision, Dismiss, Open on GitHub | approved or dismissed |
| `pr.needs_human` | a tracked pull request the watcher gave up on | fix · normal | Open on GitHub, Resume, Pause | the watch leaves `needs_human` |
| `job.failed` | the newest failed or dead job of each command, among the newest 50 | fix · urgent | Retry, Acknowledge, Open job | a newer job of the same command completed, or acknowledged |
| `access.finding` | an open finding of the access self-audit | fix · urgent (`unexplained`), else normal | Open the access pull request, Audit now, Open access | the next audit no longer reports it |
| `update.available` | a newer release in the feed | later · info | Apply (when the updater runs) or Open updates, Dismiss | applied, or dismissed until the next release |
| `setup.incomplete` | no read role, no GitHub connection, no alert queue, no model | fix · urgent (account, GitHub), normal (queue), info (model) | Open settings; "Running model-free on purpose" | the thing is configured |

## The item

```json
{
  "id": "decoy.approval:1792670000000-7f3a:ecs_restart_task",
  "kind": "decoy.approval", "verb": "approve", "severity": "urgent",
  "captain": "honeypot", "tab": "honeypots",
  "title": "Ecs restart task on payments-api for incident 00-7f3a",
  "summary": "risk 3 · chosen by the agent · parked: mode is approve",
  "since": 1792670000000,
  "subject": {"incident_id": "…", "action_id": "…", "action_type": "ecs_restart_task"},
  "actions": [
    {"key": "run", "label": "Run", "method": "POST", "path": "/api/honeypots/incidents/…/approve",
     "body": {"action_id": "…"}, "scope": "operate", "confirm": "Runs ecs_restart_task on payments-api (risk 3)."},
    {"key": "decline", "label": "Decline", "method": "POST", "path": "/api/honeypots/incidents/…/decline", "body": {"action_id": "…"}, "scope": "operate"},
    {"key": "open", "label": "Open incident", "href": "/workspace?tab=honeypots&view=history&incident=…"}
  ],
  "detail": {"risk": {"score": 3, "breakdown": {"blast_radius": 0.4}, "notes": ["env=prod:+1"]}, "reason": "mode is approve",
             "context": {"service_arn": "…", "task_arn": "…"}, "incident": {"opened_at": "…", "honeytoken": "…", "source_ip": "203.0.113.9"}},
  "snooze": null
}
```

The id is deterministic, so a client tells new from known across polls. `since`
is when the state began, in epoch milliseconds; the feed sorts on it. `actions`
carry the method, path and body of the route the tab already calls, the token
scope it needs, and the one sentence to show before a second click; `href`
actions are links. `detail` is what the tab would show before the same click,
as data. Nothing in an item is a secret.

## The API

```
GET  /api/inbox?tab=&kind=&verb=&include_snoozed=true&fresh=true     scope: read
POST /api/inbox/{id}/snooze                                            scope: operate
DELETE /api/inbox/{id}/snooze
POST /api/honeypots/incidents/{incident_id}/decline  {"action_id": …}   scope: operate
```

```json
{"generated_at": 1792670100000,
 "counts": {"total": 6, "urgent": 2, "by_tab": {"honeypots": 2, "prs": 1, "queue": 1, "settings": 2},
            "by_verb": {"approve": 2, "fix": 3, "merge": 0, "later": 1}},
 "items": [ … ]}
```

The list is computed from the stores and cached in-process for five seconds per
workspace, so a sidebar poll and a feed poll cost one computation; `fresh=true`
skips the cache. `GET /api/status` carries an `inbox` section with the counts,
the three newest items and the line `compass status` prints
(`Needs you: 6 (2 urgent): 2 to approve, 3 to fix, 1 later`).

## In the dashboard

Beside every tab in the sidebar, the tab's count: red when something in it is
urgent, amber otherwise, absent at zero. Beside the brand at the top, the total,
which opens the whole feed (`?tab=inbox`, newest first, filtered by verb); on a
phone the total sits on the menu button. The Decoys, Pull requests and Queue
tabs each have a "Needs you" view that lists their own items; Settings opens
with its items when it has any. An item's primary action is a button, the rest
are behind "⋯"; an action with a confirmation shows the sentence and asks
again; the result replaces the row for five seconds, then the feed is what it
is. Expanding an item shows what the tab would show before the same click.
Dismissing the update banner is the `update.available` item's snooze.

## Snooze

Only the kinds whose item has a natural end: `job.failed` (acknowledge; a later
failure of the same command is a new item), `update.available` (dismiss until
the next release) and `setup.incomplete:model` (running model-free on purpose).
A snooze is a row `INBOX#SNOOZE#<id>`; the id carries the end, so nothing needs
a timer. Everything else is acted on: an approval is run or declined, a grant
accepted or declined, a finding fixed.

## Contributing items

A captain names a function in its manifest, `inbox="captains.<name>.inbox:items"`,
that takes the inbox's `Context` (the items table, the workspace's config, and
fetchers for the jobs, the PR registry and the update check) and returns items
in this shape; see [writing_a_captain.md](writing_a_captain.md) §6.1. A product
captain that is not a distribution of its own registers the same function with
`compass_api.extensions.register_inbox_source(name, fn)`. A source that raises
is logged and skipped; it never breaks the list.
