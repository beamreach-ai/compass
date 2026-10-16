---
name: compass-connect-account
description: Connect an AWS account for Compass to scan, either the account Compass runs in or another account through a cross-account role. Use when the user wants Compass to scan, map or audit an account, or asks how to give Compass access.
---

# Connect a target account

1. `compass_account get`. If a target is already connected, say which and ask whether to replace it (Compass has one target per workspace today).
2. Same account as the install? → `compass_account connect` with `mode: local_iam` (plan: nothing to deploy). Done; it queues the first map and audits.
3. Another account:
   - Ask which tier: `read-only` (map, audit, FinOps; default) or `remediate` (also lets honeytoken alerting and drift fixes write tagged resources).
   - `compass_account_role_template` with the tier. It returns a quick-create link and a CLI command. Either the user runs one of those with that account's credentials, or, if they give you an AWS profile for the target account, call it again with `deploy: true`, show the plan, get a yes, then `confirm: true`.
   - When the role exists: `compass_account test` with `mode: assume_role`, `account_id`, `role_name`. On success call `connect` with the same arguments.
4. `compass_jobs list` with `captain: mapper` to confirm the infra map started. Triage and fixes stay locked until it completes; `compass_jobs wait` on it if the user wants to stay.

Errors: `AccessDenied` on test means the role is missing or the external id differs; show the external id from `compass_settings get` → `aws.external_id` and the trust principal (`aws.compass_account_id`, `aws.worker_role_name`).
