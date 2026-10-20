---
name: compass-arm
description: Arm an account with honeytokens (decoy credentials and resources that alert when touched), from the infra map's suggestions. Use when the user asks to plant honeypots, canaries, decoys or honeytokens, or to "arm" the account.
---

# Arm the account with honeytokens

Honeytokens are planted through Terraform pull requests in the user's repo and arm once merged. Alerts go to notification endpoints.

1. `compass_status`. If GitHub is not connected, connect it first (`compass_github connect`, with a token or the App link): without it there is no infra map, so no suggestions, and plant PRs cannot open. If the infra scan is not complete, wait for it (`compass_jobs wait` on the mapper job).
2. `compass_notify_endpoints list`. No endpoints → create one first (`compass-notify`), otherwise alerts go nowhere.
3. `compass_honeypots suggest`. Summarise the top suggestions by class and resource, and the reasons.
4. Ask which to plant. Default: the top 10 by priority. Let them exclude or add.
5. `compass_honeypots_plant` with `top_n` or `suggestion_resource_ids` (and `notification_endpoint_ids` if they chose), no confirm. Show the plan.
6. On yes: same call with `confirm: true`. Report what was planted and the PR links from `compass_prs` (`captain: honeypot`).
7. Offer one test: `compass_honeypots_manage test_fire` on one token with `confirm: true` only after telling them it pages the endpoints.
8. For single custom tokens, use `compass_honeypots_plant` with `honeytoken_class` and a name; the response contains the lure material once. Tell the user where to place it; never repeat the material in later messages.

Mention: tokens show `pending_pr` until the PR is merged; the user can `compass_honeypots_manage arm` after merging, or Compass marks them armed when the alerting resources appear.

Responses are grants: a response action runs only once its own pull request (the access module) is merged, applied and verified, whatever its mode says. `compass_access list` shows each one's state; an incident while a response is `pr_open` records `wait_for_grant` and does nothing. Say so before a drill, and never merge an access pull request yourself.
