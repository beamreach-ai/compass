---
name: compass-iac-adopt
description: Bring unmanaged AWS resources under Terraform and fix drift, via pull requests from the Compass infra map. Use when the user asks about IaC coverage, importing resources into Terraform, or drift.
---

# Adopt resources into Terraform

1. `compass_iac coverage` for the numbers; `compass_iac unmanaged` (filter by `resource_type` or `search`) to list candidates; `compass_iac suggestions` for repos and module paths Compass knows.
2. Ask: which resources, which repo (owner/name), base branch, module path (existing or new).
3. `compass_iac_import` without confirm → plan. Yes → `confirm: true`. The job clones the repo, generates config, rewires references, verifies `terraform plan` shows imports only, and opens a PR.
4. Drift: `compass_iac drifted`, then `compass_iac_fix_drift` with the ids (plan, yes, confirm). One PR per owning repo.
5. `compass_prs` with `captain: iac` for links and job status. Needs GitHub connected.
