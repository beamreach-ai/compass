# The patching captain: every CVE, every affected workload, a fix pull request, autonomously

Status: draft 2026-10-22, for review. Roadmap angle 12 (items 12.1–12.10).
Line references are to `main` at c259dd5; web sources as of 2026-10-22.

## 1. Goals and non-goals

**Goals.**

- Compass follows the CVE feeds, knows which of the account's workloads and
  which of the organisation's repositories are affected, opens the fix pull
  request for each, proves it (CI, an image build, a canary), and keeps the
  pull request alive until a person merges it. Nothing in that sentence
  needs a click; the merge does.
- The claim the launch post makes is exactly that: *Compass finds, locates,
  fixes and verifies; you merge.* Every word of it is true in the open image.
- "Affected" is computed from what the map already holds: the images task
  definitions run, the Dockerfiles that build them, the Lambda runtimes and
  layers, the AMIs, the lockfiles and manifests in the repositories, and
  whether the workload faces the internet.
- Matching is pure Python over public feeds that need no account or key
  (OSV, the CVE list, KEV, EPSS, the distro trackers). Amazon Inspector and
  GitHub's Dependabot alerts enrich the picture when they are on; nothing
  depends on them.
- Exploited or exposed vulnerabilities get their pull request without a
  click; the rest become inbox items a person approves (4.7). The PR
  watcher's risk policy and the access model's grants are the only two
  controls; the captain adds none of its own.
- php2shell (CVE-2026-109618, CVE-2026-109619) is the worked example: a PHP
  binary version, not a package, so the locator reads `FROM php:8.3-fpm`,
  `apt-get install php8.3-fpm`, Bref layer ARNs and AMIs, and the fix is a
  rebuild-and-redeploy pull request.

**Non-goals (for now).**

- Interim mitigations when no fix exists (12.7) and model-written patches
  to first-party code (12.8): later items, designed for in the fix
  strategy table, not built.
- Sites outside AWS and outside the mapped repositories (decision taken).
- Reachability analysis. For runtime CVEs the vulnerable function is the
  request parser; for library CVEs, KEV, EPSS and internet exposure decide,
  as the better peers' precomputed fallback does.
- Patching a running image in place (Copacetic-style). The pull request
  changes the Dockerfile or the Terraform; the image is rebuilt from code,
  so the IaC stays the source of truth.
- Replacing Dependabot. Where Dependabot is on, its alerts are a source and
  its pull requests are recognised, not duplicated (open decision 4).
- Auto-merge. "Low-risk bumps merge themselves when CI is green" is 9.2;
  this captain produces the pull requests 9.2 would merge.

## 2. Current state

**What the map knows.** The GitHub collector walks the installation's
repositories (`captains/mapper/github_mapper.py:250-262`) and classifies
only workflows, Terraform and Dockerfiles (`:430-439`); a Dockerfile node
carries its `FROM` images as `base_images` (`:492-502`), and Dockerfiles and
workflows are linked to ECR repositories by URI (`:814-832`). No lockfile,
manifest or package line is read. On the AWS side a task definition node
records container *names* but not images, although the describe call that
builds it holds them (`captains/mapper/aws_mapper.py:1306-1324`,
`_task_definition_cache`); a Lambda records its `runtime` (`:812-830`); an
EC2 instance records type, state and zone and **no AMI** (`:775-800`). Load
balancers record their `scheme` (`:642`) and `exposes` edges exist (`:1244`),
so "internet-facing" is answerable for ECS behind an ALB; security groups
open to `0.0.0.0/0` are collected (`ec2_security_group` nodes) but not
flagged. The synthetic world (`infra_map/synthetic.py:196-469`) generates
repositories and ownership but no Dockerfiles, images or runtimes.

**What opens and keeps pull requests.** `compass_core/github_prs.py`
(branch, commit files through the API, open-or-update) and
`captains/iac/repo_checkout.py` (clone, write, branch, commit, push); the
IaC pipeline is the full pattern (`captains/iac/import_pipeline.py:305-365`).
The PR watcher syncs every pull request Compass opened, reads the head's
check rollup (`captains/prs/github_pr_client.py:202-267`), raises
`pr.ci_failed` on a failing check of a head it pushed
(`captains/prs/sync.py:180-185`), marks ready when checks are green and
nothing is requested (`:217-219`), revises on review inside
`patch_spec.allowed_files` with the intent guard (`captains/prs/claude_runner.py:114-124`,
`intent_guard.py:134`), and flags a person when it cannot
(`captains/prs/captain.py:39`, `:227-236`). Its policy is `mode`, `risk_max`,
`max_revisions` (`captains/prs/policy.py:13-46`).

**What gates and grants.** `policy.gate(mode, risk, risk_max)`
(`compass_core/policy.py:264-276`) with the deterministic risk score
(`:211-246`); actions registered per captain with `iam_statements`,
`snapshot`/`execute`/`verify`/`rollback` (`compass_core/actions.py:24-107`);
grants that arm an action only after its pull request merged and IAM
confirms it (`compass_core/grants.py:8-18`). A captain declares its reads
and its actions in `permissions` (`captains/iac/manifest.py:24`).

**The hosted neighbours.** `audit` runs Trivy, Prowler, Checkov and Gitleaks
(`beamreach-coworker/src/beamreach/captains/audit/captain.py:69-88`) and
`engine` plans and opens the fix for a finding with a model
(`engine/captain.py:465`); both cloud-tier (`compass_core/catalog.json`).
They keep code fixes, compliance and reachability; this captain is the
open, feed-driven, deterministic part underneath.

**The field.** Dependabot bumps to the lowest patched version, groups per
ecosystem, never bumps a transitive outside npm, and offers no security
updates for Docker or Terraform. Renovate lets security pull requests skip
schedules and merges only after required checks. Snyk, Aikido, Pixee,
ZeroPath, Mobb, CodeMender, Claude Code Security and OpenAI's Aardvark all
stop at a reviewed pull request; the strongest prove the fix first (CI,
fuzzing, a sandboxed exploit). Amazon Inspector maps ECR images to the ECS
tasks that run them (`ecrImageInUseCount`, since May 2025) and Lambda
managed runtimes are patched by AWS while container-image functions are the
customer's. Sources in Appendix A.

## 3. The design

```
feeds (hourly)                inventory (per map scan)            findings            fix PRs (per repo)
 OSV modified_id.csv          task definition → image → ECR        purl × advisory     Dockerfile FROM / apt / apk
 cvelistV5 hourly delta       Dockerfile FROM, apt/apk lines  ──▶  + exposure     ──▶  Bref layer / runtime / AMI
 KEV · EPSS                   Lambda runtime, layers, Bref         + owner             manifest + lockfile bump
 Ubuntu · Debian · Alpine     EC2 AMI (+ SSM inventory when on)    + fixed version     one PR per repo, grouped
 ALAS · Packagist · npm       lockfiles, manifests in each repo         │                    │
 (+ Inspector, Dependabot)                                              ▼                    ▼
                                                           policy: exploited or exposed → PR now
                                                                   else → inbox item          verify: CI · image build · canary (grant)
                                                                                              the PR watcher keeps it alive → a person merges
```

### 3.1 Vocabulary

- **Advisory** — one vulnerability as the feeds describe it: ids (CVE, GHSA,
  distro ids), affected and fixed ranges per package or product, severity,
  KEV and EPSS, references, published and modified times.
- **Software** — what a workload or a repository is made of, as purls:
  `pkg:docker/php@8.3.30-fpm`, `pkg:deb/ubuntu/php8.3-fpm@8.3.6-0ubuntu0.24.04.12`,
  `pkg:composer/laravel/framework@11.2.0`, `pkg:generic/aws-lambda-runtime@python3.10`,
  `pkg:generic/bref-layer@php-83-fpm:51`, `pkg:generic/ami@ami-0abc…`. Kept as
  facts on the node they describe, under the source that found them.
- **Finding** — advisory × software × node, with the fixed version, the
  owning repository and file, the workload's exposure, and the policy's
  verdict. Deterministic id `<advisory>:<node>:<purl>`.
- **Exposed** — the workload is reachable from the internet: behind an
  internet-facing load balancer, a Lambda function URL or API Gateway, a
  public IP with a security group open on its port, or CloudFront.
- **Fix** — one edit that moves software to a fixed version, by strategy
  (section 3.5). **Fix PR** — every fix for one repository, grouped.
- **Canary** — the fix's image run as one task of a named staging service
  for a few minutes and probed, through a granted action (section 3.6).

### 3.2 The feeds (12.1)

A core service `cve-feed` (`ServiceSpec`, hourly, `interval_setting="cve_feed"`):

| Source | What it gives | How |
|---|---|---|
| OSV.dev | the union of GHSA, PyPA, Go, RustSec, Debian, Alpine, Ubuntu, Packagist and NVD-for-OSS as OSV JSON | `gs://osv-vulnerabilities/<ECOSYSTEM>/modified_id.csv` for deltas, then `GET /v1/vulns/{id}`; `POST /v1/querybatch` by purl for the inventory's software; no key, no limit |
| cvelistV5 | every CVE, with CISA's ADP enrichment (SSVC, KEV, CVSS) | the hourly delta zip; `git clone` is not used |
| CISA KEV | known exploited | the JSON catalog; the `catalogVersion` is the cursor |
| EPSS | exploitation probability | `api.first.org/data/v1/epss?cve=…` for the findings' CVEs, daily |
| Ubuntu, Debian, Alpine, ALAS | "which package version in which release is fixed", which the ecosystem feeds cannot answer for a distro's `php8.3-fpm` | Ubuntu `security/cves/<CVE>.json`; Debian tracker per CVE; Alpine `secdb/<release>/main.json`; ALAS per-CVE page (no JSON; parsed, cached a day) |
| Packagist, npm | PHP and JS advisories at the source, for freshness | `packagist.org/api/security-advisories/?updatedSince=`; npm bulk advisories |
| Amazon Inspector (optional) | findings per ECR image, with `ecrImageInUseCount` | `inspector2:ListFindings` when the read role has it (declared, used when present) |
| Dependabot alerts (optional) | per-repository alerts with `first_patched_version` | `GET /repos/{r}/dependabot/alerts` when the token or App has `security_events:read` |

Advisories are stored in the items table (`CVE#<id>`) with the sources that
mentioned them and a `modified` cursor per source; a run fetches only what
changed. The first run fetches nothing historical: it queries OSV by the
inventory's purls (section 3.3), so a fresh install is current in minutes,
not after a day of backfill.

### 3.3 The inventory (12.2) and the match (12.3)

The mapper gains three collectors, all under `source = "software"`, scoped
per node, so a rescan replaces a node's software and nothing else:

- **AWS.** From the describe calls the mapper already makes: the image of
  every container in a task definition (`containerDefinitions[].image`,
  resolved to the ECR repository and the digest the tag points at, through
  `ecr:DescribeImages`); a Lambda's runtime, layers (Bref layer ARNs carry
  their version) and, for container-image functions, the image; an EC2
  instance's AMI and, when SSM Inventory is on, its installed packages
  (`ssm:ListInventoryEntries`, `AWS:Application`). Exposure from the map:
  the ALB `scheme`, function URLs, API Gateway, public IPs and security
  group rules, CloudFront origins.
- **GitHub.** Three new file classes beside workflows, Terraform and
  Dockerfiles: manifests (`composer.json`, `package.json`, `requirements*.txt`,
  `pyproject.toml`, `go.mod`, `Cargo.toml`), lockfiles (`composer.lock`,
  `package-lock.json`, `yarn.lock`, `pnpm-lock.yaml`, `uv.lock`, `poetry.lock`,
  `go.sum`, `Cargo.lock`), and deploy descriptors (`serverless.yml`, `samconfig`,
  Terraform `aws_lambda_function` layers and `aws_instance` AMIs). Dockerfiles
  are read fully: `FROM` with tags and digests per stage, `apt-get install`,
  `apk add`, `pip install`, `composer require` lines with their pins. Each
  file yields software facts on the file node; the repository's ownership
  edges already tie a Dockerfile to the ECR repository and the task
  definition to the service.
- **The match.** For every software fact, the OSV batch query by purl, plus
  the distro trackers for OS packages and the docker-library tags for
  `pkg:docker/php`, `node`, `python`, `ruby`, `nginx`, `httpd` (the fixed
  tag is the one whose upstream version is fixed). A match is a finding
  with the fixed version from the advisory, the owning repository and file
  from the ownership edges, exposure from the node, and KEV, EPSS and CVSS
  from the advisory. Transitive dependencies in lockfiles are matched; the
  fix targets the direct dependency whose bump brings the fixed transitive,
  as Dependabot does for npm and Mend for the rest, else the finding says
  "transitive, no direct bump" and stays an inbox item.

Findings are rows (`FINDING#<id>`) with their state: `open`, `pr_open`,
`merged`, `deployed` (the map shows the fixed version running), `ignored`
(a person, with a reason, and until when). A finding closes itself when the
inventory no longer matches.

### 3.4 The policy (12.5)

Two outcomes, decided per finding at match time and re-decided when KEV,
EPSS or exposure change:

- **A pull request now**, when any of: in KEV; EPSS ≥ 0.1; CVSS ≥ 7.0; the
  workload is exposed. The captain opens the fix PR for the repository
  without a click.
- **An inbox item** otherwise: "Open the pull request for 4 findings in
  acme/api", verb `approve`, one per repository, with the findings listed;
  accepting opens the same PR.

Beyond that, the two controls that exist: the PR watcher's policy decides
how the pull request is revised and when it is called ready (`prs.watcher`),
and the grant flow decides whether the canary may run. The captain has no
mode of its own; `off` is the captain turned off. php2shell qualifies on
three counts on day one (CVSS 9.2, exposed, and EPSS once it is scored).

### 3.5 The fixes (12.4)

One pull request per repository, grouping every fix that repository needs,
on branch `beamreach/patch/<yyyymmdd>-<short>`, regenerated (not appended)
when new findings land before it merges. Strategies, in the first release:

| Software | The edit | Verified by |
|---|---|---|
| `FROM php:8.3.30-fpm` (any docker-library tag) | the same tag line at the fixed upstream version (`php:8.3.36-fpm`); a digest-pinned `FROM` gets the new digest beside the tag | CI; the image build; the canary |
| `apt-get install php8.3-fpm` (unpinned) on an Ubuntu/Debian base | pin to the fixed distro version (`php8.3-fpm=8.3.6-0ubuntu0.24.04.13`) when the base tag is older than the fix, else bump the base tag so the repository's package is current | the same |
| `apk add php83` on Alpine | the same, with Alpine's secdb version | the same |
| Lambda runtime (`python3.10` deprecating) | the next supported runtime in the Terraform or `serverless.yml`, with the handler untouched | CI; the canary when the function has a staging alias |
| Bref layer ARN / version | the fixed Bref release's layer (`bref/php-83-fpm:52`) | the same |
| Container-image Lambda | as the Dockerfile row | the same |
| EC2 AMI in Terraform (`aws_instance`, launch templates) | the newest AMI of the same family with the fix (from SSM public parameters for AL and Ubuntu; a filter on `aws_ami` data sources keeps the choice in code) | CI (plan) |
| Manifest + lockfile (composer, npm, pip, go, cargo) | the lowest fixed version of the direct dependency in the manifest; the lockfile regenerated (open decision 1) | CI |

Every edit is deterministic: a version string replaced by another the
advisory names. The model writes nothing in a fix. It writes the pull
request body's one-paragraph explanation of the CVE and why this workload
is affected, from the advisory and the finding, with the facts quoted and
the links; without a model the body is the template. The body lists each
finding (CVE, package, from → to, the workload and whether it is exposed,
KEV/EPSS/CVSS), what verification ran and its result, and how to roll back
(revert the PR). Labels: `compass:patch`, `security`, and `compass:exploited`
when KEV.

What the first release does not do: bump major versions (a finding whose
only fixed version is a major is an inbox item with that said), touch a
repository with an open Dependabot pull request for the same package
(section 3.9), or edit anything but the files the strategy names
(`patch_spec.allowed_files` is exactly those files, so the watcher's
revisions stay inside them too).

### 3.6 Verification (12.6)

Three steps, each recorded on the finding and in the PR body, each one
able to stop the pull request from being called ready:

1. **CI.** The repository's checks on the PR's head, as the watcher reads
   them. Red → `needs_you` with the failing check's text.
2. **The image build.** Where the fix touched a Dockerfile, the image must
   build. Compass has no Docker socket by design (the updater alone holds
   it), so the build runs where the repository builds: the captain looks
   for a workflow that builds and pushes the image on pull requests and
   reads its check; where none exists, the PR adds a minimal
   `.github/workflows/compass-build.yml` that builds the changed Dockerfile
   with BuildKit and, if the repository's existing workflow pushes to ECR,
   pushes with the tag `pr-<sha>` (open decision 2). "Built" is a check
   on the PR like any other.
3. **The canary.** For a finding on an ECS service, when the workspace has
   named a staging service for it (`patch.canaries[]`, a mapping from
   service to its staging twin) and the grant `patch.canary_deploy` for
   that staging service is armed: register a task definition revision with
   the `pr-<sha>` image, update the staging service to it, wait for steady
   state, probe (`GET /` on the target group's health path and up to three
   URLs named in the config, expecting the status codes the service answered
   before), compare 5xx and p95 latency with the five minutes before, then
   roll the staging service back to its previous revision. The action is
   registered through `compass_core.actions` with `iam_statements` scoped to
   the named staging services and clusters (`ecs:RegisterTaskDefinition`,
   `ecs:UpdateService`, `ecs:DescribeServices`, `iam:PassRole` for the task
   roles, `elasticloadbalancing:Describe*`, `cloudwatch:GetMetricData`),
   `snapshot` (the current revision), `execute`, `verify` (steady state and
   the probes) and `rollback` (the previous revision), so it goes through
   the grant flow like a decoy response. Without a staging twin or a grant,
   the canary is skipped and the PR says so; the PR is still ready on CI
   and the build.

"Ready" means all three that applied passed. The watcher's existing
`needs_human` reasons cover a failed step; a failed canary adds
`canary_failed` with the probe that failed.

### 3.7 Keeping it alive (12.4)

The pull request is a tracked PR like the IaC captain's: the watcher syncs
it, answers reviewers, revises within `allowed_files`, nudges after
`nudge_after_days`, and marks `needs_human` when a review asks for something
outside the fix. A merged fix PR moves its findings to `merged`; the next
map scan that sees the fixed version running moves them to `deployed`, and
the finding's row says when. A finding whose PR was closed without merging
goes back to `open` with "closed by <login>" and opens no new PR for 14
days unless the advisory changes (KEV added, EPSS up).

### 3.8 Where it shows (12.9)

- **A Patches tab** (`TabSpec(key="patches", group="security")`): findings
  by workload and by repository, with exposure, severity, KEV/EPSS, the fix
  PR and its verification, and the deployed state; a filter for "exposed",
  "exploited", "waiting for merge"; the feed's last run and the inventory's
  coverage ("37 of 41 images resolved; 4 task definitions use images
  outside ECR").
- **Inbox kinds**: `patch.approve` (open the PR for N findings in a repo,
  verb `approve`), `patch.needs_you` (a fix PR the watcher gave up on,
  `fix`), `patch.deploy` (a fix merged a week ago and still not running,
  `fix`, with "redeploy" as the text). Counts land on the Patches tab.
- **CLI and MCP**: `compass patch findings [--exposed] [--exploited]`,
  `compass patch pr <repo>`, `compass patch ignore <finding> until=… reason=…`;
  MCP `compass_patch_findings` (read) and `compass_patch_pr` (operate).
  `compass status` gains "Patches: 3 findings on exposed workloads, 2 PRs
  waiting for merge".
- **Notifications**: `patch.pr_opened` (medium; high when KEV),
  `patch.needs_human`, `patch.deployed` (info), through today's routing.

### 3.9 Living with Dependabot and Inspector

- A repository with Dependabot security updates on: the captain reads the
  alerts as a source, recognises Dependabot's pull request for the same
  package (title and branch pattern) and attaches it to the finding instead
  of opening a second; the watcher keeps Dependabot's PR alive as it would
  its own (open decision 4).
- Inspector on: its findings for ECR images are matched to the same
  software facts and add nothing new except confidence and
  `ecrImageInUseCount`; Compass's own map remains the owner of "which
  service runs it".

### 3.10 The captain's manifest

```python
MANIFEST = CaptainManifest(
    name="patch", label="Patches",
    summary="Follows the CVE feeds, finds every affected workload and repository, opens and verifies the fix pull request.",
    tier="open", feature="patches",
    commands="captains.patch.commands",
    routers=("compass_api.routes.patches:router",), route_prefixes=("/api/patches",),
    tabs=(TabSpec(key="patches", label="Patches", group="security", feature="patches"),),
    services=(ServiceSpec(name="cve-feed", start="captains.patch.feed:run_service", interval=3600, interval_setting="cve_feed"),
              ServiceSpec(name="patch-tick", start="captains.patch.scheduler:run_service", interval=900, interval_setting="patch_tick")),
    permissions=Permissions(
        aws=AwsPermissions(read=INVENTORY_READS + ("ecr:DescribeImages", "ecr:BatchGetImage", "lambda:GetLayerVersion",
                                                   "ssm:ListInventoryEntries", "ssm:GetParameter", "inspector2:ListFindings"),
                           actions=("patch.canary_deploy",)),
        github=("contents:write", "pull_requests:write", "security_events:read"),
        egress=("api.osv.dev", "storage.googleapis.com", "github.com", "www.cisa.gov", "api.first.org",
                "ubuntu.com", "security-tracker.debian.org", "secdb.alpinelinux.org", "alas.aws.amazon.com", "packagist.org", "registry.npmjs.org"),
    ),
    depends_on=("mapper", "prs"), requires_core=">=0.9,<1", version=__version__,
)
```

`lambda:Get*` is a write in the access model's eyes (it downloads code);
`lambda:GetLayerVersion` returns metadata and a download URL and is
declared on its own so the validator's rule stays honest: the captain reads
the layer's version, never the URL.

### 3.11 php2shell, end to end

Day 0, 14:00 UTC: the advisory lands in OSV and cvelistV5; the hourly feed
picks it up by 15:00. The inventory already holds, for a typical PHP shop:
`pkg:docker/php@8.3.30-fpm` on `acme/web`'s Dockerfile, built to
`acme-web` in ECR, run by task definition `web:41` in service `web` behind an
internet-facing ALB; `pkg:deb/ubuntu/php8.3-fpm@8.3.6-0ubuntu0.24.04.12` on
`acme/legacy`'s Dockerfile; `pkg:generic/bref-layer@php-83-fpm:51` on three
Lambdas from `acme/functions`' `serverless.yml`. Three findings, all exposed,
CVSS 9.2: pull requests now.

- `acme/web`: `FROM php:8.3.30-fpm` → `FROM php:8.3.36-fpm`. CI runs; the
  build workflow pushes `pr-<sha>`; the canary runs it on `web-staging` for
  five minutes, probes `/` and `/health`, compares errors, rolls back. Ready
  by 15:40. The body says: two CVEs, pre-auth RCE on any PHP request parser,
  exploited in the wild before disclosure, this service is internet-facing,
  from 8.3.30 to 8.3.36, verified by CI, build and canary; revert to roll
  back.
- `acme/legacy`: the base is `ubuntu:24.04` with an unpinned
  `apt-get install php8.3-fpm`; the fix pins `php8.3-fpm=8.3.6-0ubuntu0.24.04.13`
  (USN-8948-1). CI and build; no staging twin, so no canary, and the body
  says so.
- `acme/functions`: `bref/php-83-fpm:51` → `:52` (Bref 3.0.14). CI; the
  staging alias canary if configured.

A person merges three pull requests. The next map scan sees 8.3.36 running
and marks the findings `deployed`. An install where the only PHP is on an
EC2 instance under SSM gets the AMI or the package finding the same way; one
where PHP is on a host Compass cannot see gets nothing, and the post does
not claim otherwise.

### 3.12 The demo replay and the post (12.10)

The synthetic world gains PHP: a `web` service with a Dockerfile
`FROM php:8.3.30-fpm`, a legacy service on Ubuntu with the apt line, three
Bref functions, two of them exposed, in synthetic repositories the demo's
fake GitHub holds (the demo already runs a sandbox repository; the synthetic
repositories' files are seeded there). `compass demo replay php2shell`
inserts the advisory into the feed's store as of now, runs the tick, and
the three pull requests open on the sandbox repo; the demo's tour gains an
optional step, "Watch a CVE get patched". The recording pipeline's beat
`patch.js` records: the feed line, the Patches tab filling, the PRs, the
canary's timeline, one merge, the finding turning `deployed`. The post's
claim, verbatim: "Compass follows the CVE feeds, finds every affected
workload and repository, opens the fix pull request, verifies it with your
CI, a build and a canary, and keeps it alive until you merge. php2shell
landed at 14:00; by 15:40 the pull requests were ready." Every sentence is
a line in section 6's tests.

## 4. Configuration

```json
"patch": {
  "enabled": true,
  "act_on": {"kev": true, "epss_min": 0.1, "cvss_min": 7.0, "exposed": true},
  "repos": [],                       # empty: every repository the mapper sees
  "canaries": {"web": {"staging_service": "web-staging", "cluster": "staging", "probe_paths": ["/", "/health"], "minutes": 5}},
  "max_open_prs": 10,                # per workspace; beyond it, inbox items
  "lockfiles": "ci",                 # open decision 1: ci | toolchains | manifest-only
  "ignore": [{"advisory": "CVE-…", "until": "2026-12-01", "reason": "…"}]
}
```

The PR watcher's policy (`prs.watcher`) applies to the captain's pull
requests unchanged; `prs.watcher.repos` limits where it opens them.

## 5. What changes where

- **Mapper.** The three collectors of section 3.3 (`captains/mapper/software.py`,
  `github_mapper.py` file classes), image resolution through ECR, AMIs on
  instances, exposure as a node attribute (`exposed`, with the evidence
  edge). The synthetic world gains Dockerfiles, images, runtimes and
  exposure.
- **New captain** `captains/patch/`: `feed.py` (sources, cursors, the
  advisory store), `inventory.py` (purls from facts), `match.py`,
  `policy.py` (the two outcomes), `fixes/` (one module per strategy),
  `verify.py` (CI, build, canary), `actions/canary_deploy.py`, `pr.py`
  (grouping, body, labels), `scheduler.py`, `commands.py`, `manifest.py`.
  `compass_api/routes/patches.py`. The Patches tab in the dashboard.
- **Core.** `compass_core/keys.py` (`CVE#`, `FINDING#`); inbox kinds
  (4.7); notification kinds; `compass_core/catalog.json` is unchanged
  (first-party, in the image).
- **CLI.** `cli/src/tools/patch.ts`, the MCP tools, `status.ts`, the
  `compass-patch` skill. `tests/compass_core/test_core_cli.py` pins `patch`.
- **Docs.** `docs/patching.md` (what it follows, what it fixes, what it
  verifies, how to name a canary, what it never does); `docs/architecture.md`;
  `docs/access.md` (the canary grant); `README.md`'s table gains a row; the
  `compass-open` skill a step; `CHANGELOG.md` under the next release.
- **Demo** (coworker): the PHP workloads in the world, `demo replay
  php2shell`, the tour step, the beat.

## 6. Test strategy

- **Feeds** (`tests/captains/patch/test_feed.py`): each source parsed from
  recorded fixtures (OSV JSON, cvelistV5 JSON, KEV JSON, EPSS, Ubuntu CVE
  JSON, Debian tracker HTML, Alpine secdb, ALAS HTML, Packagist); cursors
  advance; a second run fetches nothing; network refused (`responses`).
- **Inventory** (`test_inventory.py`, moto): task definitions with ECR
  images resolve to digests; Lambda runtime and Bref layers; AMIs; SSM
  inventory when present; Dockerfile parsing for every strategy's line
  shapes; manifests and lockfiles per ecosystem from fixtures; exposure for
  each path (ALB scheme, function URL, public IP + SG, CloudFront).
- **Match** (`test_match.py`): php2shell against the inventory of section
  3.11 yields exactly three findings with the right fixed versions; a
  transitive with and without a direct bump; a major-only fix stays an
  inbox item; an ignored advisory; the finding closes when the inventory
  changes.
- **Policy**: each of the four triggers alone; none → inbox item.
- **Fixes** (`test_fixes.py`): golden diffs per strategy, including
  digest-pinned `FROM`, multi-stage Dockerfiles, an apt line with other
  packages, Terraform AMI filters, each ecosystem's manifest; the
  `allowed_files` set equals the touched files; the body template.
- **Verify** (`test_verify.py`, moto): the canary action's statements,
  snapshot, execute, verify (steady state, probes recorded through a stub),
  rollback; refused when the grant is not armed; skipped without a twin.
- **The PR** (fake GitHub from `e2e/fakes`): one PR per repo, regenerated
  on new findings, labels, the Dependabot-PR recognition.
- **e2e** (`e2e/test_15_patch.py`): the fixture repository gains a
  Dockerfile `FROM php:8.3.30-fpm` and a `composer.lock`; the advisory is
  injected through `patch feed inject` (a test-only command behind the e2e
  flag); the Patches tab shows the finding as exposed; the PR opens on the
  fake GitHub with the golden diff; merging in the fake and rescanning marks
  it `deployed`; the inbox item appears for a non-exposed finding.
- **The live claim** (coworker, by hand before the post): the replay on
  demo.beamreach.ai, timed.

## 7. Risks

- **False positives from version strings.** A `FROM php:8.3` floating tag
  means "whatever was pulled"; the inventory resolves the running image's
  digest through ECR and the registry's tag history, and a finding on a
  floating tag says "resolved to 8.3.30 on <date>". A distro package with a
  backported fix keeps an old upstream version number; the distro trackers
  are consulted before the upstream range, as Grype and Trivy do.
- **Lockfile regeneration** needs toolchains the image does not have
  (open decision 1); the first release may ship manifest edits with CI
  regenerating the lockfile.
- **Noise.** `max_open_prs` and grouping per repository bound it; the
  policy's four triggers keep the autonomous set small; everything else
  waits in the inbox.
- **The canary writes to the account.** Only through a granted action
  scoped to named staging services, with rollback; never to the service
  that serves traffic.
- **A fix that breaks the build.** CI and the build check catch it before
  "ready"; the watcher's revision loop may try the next fixed version once
  (e.g. 8.3.36 → 8.3.37) within `allowed_files`, then `needs_you`.
- **Feed outages or format changes.** Each source is independent; one
  failing is logged and the others run; the Patches tab shows each
  source's last success.
- **Overlap with the hosted audit/engine.** Deliberate; section 2 says
  what stays paid.

## 8. PR plan

Each shippable alone, with a stop after each:

1. **`mapper: software facts on the map`** (12.2): images, AMIs, runtimes,
   layers, Dockerfile lines, manifests and lockfiles, exposure. *Stop: the
   inventory of the e2e world and of your account, as purls.*
2. **`patch: the feeds and the advisory store`** (12.1). *Stop: the store
   after one run against the live feeds, and the php2shell advisory in it.*
3. **`patch: findings`** (12.3, 12.5): match, policy, the rows, the Patches
   tab read-only, the inbox kinds. *Stop: findings on the e2e world.*
4. **`patch: fix pull requests`** (12.4): the strategies except lockfiles,
   grouping, the body, the watcher tracking them. *Stop: the three
   php2shell PRs on the fake GitHub.*
5. **`patch: verification`** (12.6): CI, the build workflow, the canary
   action and grant. *Stop: a canary on a real staging service of yours,
   with the grant flow walked.*
6. **`patch: lockfile bumps`** per open decision 1.
7. **`cli: compass patch`** and the skill.
8. **Demo** (coworker): the PHP world, the replay, the tour step, the
   beat; the post.

## 9. Decisions taken (2026-10-22, from the questions)

1. Affected = workloads on the map and the repositories that build them.
2. Matching in pure Python over OSV and the distro feeds; Inspector and
   Dependabot alerts as optional sources.
3. Autonomous up to the merge: find, locate, open, verify, keep alive; a
   person merges.
4. Open edition, in the core image.
5. A pull request without a click for KEV, EPSS ≥ 0.1, CVSS ≥ 7 or an
   exposed workload; an inbox item otherwise.
6. First release: base image and runtime bumps, and dependency bumps in
   manifests and lockfiles. Interim mitigations and model-written code
   fixes later.
7. Verification: CI, an image build, and a canary deploy, the canary as a
   granted action on a named staging service.
8. The launch is the feature plus a php2shell replay on the demo.

## Open decisions

**1. Regenerating lockfiles.** (a) `ci`: the PR edits the manifest and adds
a job that regenerates the lockfile on the branch (Compass ships the
workflow); (b) `toolchains`: an optional compose service with node, composer,
pip, go and cargo that the daemon calls to regenerate locally (an image of
~1.5 GB, off by default); (c) `manifest-only`: the PR edits the manifest and
says the lockfile needs regenerating. *Recommendation: (a) for the first
release*, with Python pins done in-process (no lockfile to regenerate for
`requirements.txt`), and (b) as a later option for repositories without CI.

**2. Where the image builds.** (a) the repository's own build workflow,
and a minimal one added by the PR when none exists (specified); (b) AWS
CodeBuild through a granted action; (c) Compass builds with a rootless
BuildKit in a sidecar. *Recommendation: (a).* It keeps Compass without a
Docker socket and the artefact where the team's own builds go; (b) is the
fallback for GitHub Enterprise installs without Actions.

**3. The canary's scope.** Named staging twins only (specified) versus
"any service tagged `compass:canary`" versus a short-lived task Compass
starts itself. *Named twins*: the grant's statements name exact services,
which is what the access model promises.

**4. Dependabot coexistence.** Attach to Dependabot's PR and keep it alive
(specified) versus open Compass's own beside it. *Attach*: two PRs for one
bump is the thing people hate most about these tools.

**5. The EPSS threshold.** 0.1 (specified; roughly the top 10%) versus 0.3.
*0.1*: php2shell's first-day EPSS is unknown, which is why CVSS and exposure
are triggers too.

**6. Feed cadence.** Hourly (specified) versus every 15 minutes. *Hourly*,
with `compass patch feed run` for the day a disclosure lands.

**7. Name.** `patch` / Patches (specified) versus `cve` / Vulnerabilities.
*Patches*: the tab is about what gets fixed, not what is wrong.

## Appendix A. Sources (opened 2026-10-22)

php2shell: [Calif](https://calif.io/php2shell/) · [Wiz](https://www.wiz.io/blog/php2shell-php-rce) · [GHSA-w8hw-w9fx-j79w](https://github.com/php/php-src/security/advisories/GHSA-w8hw-w9fx-j79w) · [php.net](https://www.php.net/archive/2026.php) · [USN-8948-1](https://ubuntu.com/security/notices/USN-8948-1) · [Debian tracker](https://security-tracker.debian.org/tracker/CVE-2026-109618) · [ALAS](https://explore.alas.aws.amazon.com/CVE-2026-109618.html) · [Bref releases](https://github.com/brefphp/bref/releases) · [AWS MadPot](https://aws.amazon.com/blogs/security/amazon-threat-intelligence-identifies-php2shell-exploitation-prior-to-public-disclosure-in-madpot/) · [Qualys](https://blog.qualys.com/vulnerabilities-threat-research/2026/10/20/php-announces-critical-security-release-for-two-vulnerabilities).
Feeds: [OSV API](https://google.github.io/osv.dev/api/) · [OSV data](https://google.github.io/osv.dev/data/) · [cvelistV5](https://github.com/CVEProject/cvelistV5) · [KEV](https://www.cisa.gov/known-exploited-vulnerabilities-catalog) · [EPSS](https://api.first.org/) · [GitHub advisories](https://docs.github.com/en/rest/security-advisories/global-advisories) · [Ubuntu API](https://ubuntu.com/security/api/docs) · [Alpine secdb](https://secdb.alpinelinux.org/) · [Packagist](https://packagist.org/apidoc) · [Go vulndb](https://go.dev/doc/security/vuln/database).
Matching and fixing: [OSV-Scanner](https://google.github.io/osv-scanner/supported-languages-and-lockfiles/) · [Grype](https://anchore.com/blog/say-goodbye-to-false-positives/) · [Trivy VEX](https://trivy.dev/latest/docs/supply-chain/vex/) · [Dependabot alerts API](https://docs.github.com/en/rest/dependabot/alerts) · [Dependabot security updates](https://docs.github.com/en/code-security/dependabot/dependabot-security-updates/about-dependabot-security-updates) · [Renovate automerge](https://docs.renovatebot.com/key-concepts/automerge/) · [Mend Remediate](https://docs.mend.io/integrations/latest/mend-remediate-and-renovate) · [Snyk fix PRs](https://docs.snyk.io/scan-with-snyk/pull-requests/snyk-fix-pull-or-merge-requests) · [Aikido AutoFix](https://help.aikido.dev/aikido-autofix/autofix-for-open-source-dependencies) · [Endor reachability](https://docs.endorlabs.com/scan/sca/reachability-analysis/) · [CodeMender](https://deepmind.google/blog/introducing-codemender-an-ai-agent-for-code-security/) · [Claude Code Security](https://www.anthropic.com/news/claude-code-security) · [Copilot Autofix](https://docs.github.com/en/code-security/code-scanning/managing-code-scanning-alerts/responsible-use-autofix-code-scanning).
AWS: [Inspector ECR scanning](https://docs.aws.amazon.com/inspector/latest/user/scanning-ecr.html) · [Lambda runtimes](https://docs.aws.amazon.com/lambda/latest/dg/lambda-runtimes.html) · [Patch Manager](https://docs.aws.amazon.com/systems-manager/latest/userguide/patch-manager.html) · [Copacetic](https://project-copacetic.github.io/copacetic/website/).
