---
name: compass-notify
description: Set up and test notification channels for Compass alerts (email, Slack, PagerDuty, webhook). Use when the user asks where alerts go, wants Slack or email alerts, or asks to test notifications.
---

# Notification channels

1. `compass_notify_endpoints list`.
2. Add what the user asks for:
   - Email with zero setup: `type: beamreach_email`, `recipients`. Beamreach relays it.
   - Slack: `type: slack` with `webhook_url` (incoming webhook) or `bot_token` + `channel`.
   - PagerDuty: `type: pagerduty` with `routing_key`.
   - Generic: `type: webhook` with `webhook_url`.
   - Own SES / SMTP only if they insist; those need verified senders.
3. `compass_notify_endpoints test` on the new endpoint and ask the user to confirm it arrived.
4. Endpoints are used by honeytoken alerts and runbooks. Audit completion and PR notifications are not routed yet; say so if asked.
5. Secrets (webhook URLs, keys, tokens) are stored by Compass and never returned; do not echo them back.
