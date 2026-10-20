---
name: compass-connect-account
description: Connect an AWS account for Compass to scan, by creating the two roles Compass reads and acts through with `compass connect`, or by recording roles made from its template. Use when the user wants Compass to scan, map or audit an account, or asks how to give Compass access.
---

# Connect a target account

Compass only reads an account, through a role it creates once, and writes nothing without a pull request the user merges. The operator's AWS profile is used by one command, `compass connect`, which runs in the install's directory, not through the API.

1. `compass_account get`. If a target is connected (`aws.connected`), say which account, the roles, when it was verified, and ask whether to replace it (one target per workspace today).
2. Not connected: `compass_account connect` with `account_id` and `region` prints the command to run, with the user's profile mounted for that one run:
   `docker compose run --rm -v ~/.aws:/root/.aws:ro -e AWS_PROFILE=<profile> compass connect account=<id> region=<region>`.
   Ask which profile, and whether the Terraform state bucket should be readable (`state_bucket=`). The user runs it; it creates two IAM roles, an alert queue and a runtime user, and prints `verification`. Then `docker compose up -d`.
3. The account is out of reach from this machine: `compass_account template` with `account_id`, `region` and the base identity (`aws.runtime_arn` from `get`) writes the roles as CloudFormation and Terraform; the user applies them in that account, then `compass_account verify`.
4. `compass_account verify` after any change: assumes the roles as the runtime identity and simulates the read policy. `verification.denied` lists what the role cannot do; `simulated: false` means the simulation was not available, not a failure.
5. A role made by hand or from the template: `compass_account test` with `mode: assume_role`, `account_id`, `role_name` (and `external_id`, `act_role_name`); on success `connect` with the same arguments records it.
6. `compass_jobs list` with `captain: mapper` to confirm the infra map started; `compass_jobs wait` on it if the user wants to stay.

Errors: `AccessDenied` on verify or test means the trust policy does not name the runtime identity (`aws.runtime_arn`) with this external id (`aws.external_id`); `compass connect` writes both. The services refuse to start with `AWS_PROFILE` set or `~/.aws` mounted: that is by design, remove them.
