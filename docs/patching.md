# Patches

Compass follows the CVE feeds, knows which of the account's workloads and which
of the organisation's repositories are affected, and opens the fix pull request
for each. This page says what it follows, what it matches, what it decides, and
what it never does. The design is in
[docs/specs/patching-captain.md](specs/patching-captain.md).

## What it follows

Every hour, without a key or an account:

| Source | What it gives |
|---|---|
| OSV | advisories for the packages on the map, asked by their purls on every run, and the deltas of the ecosystems in use |
| cvelistV5 | every new CVE, kept when it names a product the account runs (PHP, Node, Python, nginx, Apache, Ruby, Go, Redis, Postgres, MySQL, MariaDB, HAProxy, Traefik, Caddy, WordPress, Drupal, OpenSSL) or is already stored |
| CISA KEV | known exploited; the catalogue version is the cursor |
| EPSS | exploitation probability for the stored CVEs, daily |
| Ubuntu, Debian, Alpine, Amazon Linux | which package version in which release is fixed |
| Packagist, npm | the PHP and JavaScript advisories at the source |

Advisories merge on the CVE id into rows of the items table (`CVE#`). A feed
that fails is logged under its name and the others run; the Patches tab shows
each one's last success. `compass patch feed_run` runs them now, the day a
disclosure lands.

## What it matches

The mapper records what every workload is made of as package URLs
(`attributes.software` on the node): the image of each container in a task
definition, a Lambda's runtime and layers, an instance's AMI and packages, and
in the repositories the Dockerfiles, manifests, lockfiles, serverless and SAM
descriptors and Terraform. Every fifteen minutes the match holds each entry
against the advisories, in the ecosystem's own version order (dpkg's for deb
and apk, rpm's, PEP 440, semver), and writes a finding per advisory, node and
package: the fixed version, the repository and file, the workloads that run
what the file builds (through the repository's image repositories on the map)
and whether any of them faces the internet.

A finding says what stands in the way of a plain bump: no fixed version yet, a
floating image tag, a transitive dependency, a major upgrade, a Bref layer
whose exact PHP patch release is not known.

## What it decides

Two outcomes, re-decided every tick as KEV, EPSS and exposure change:

- **A pull request without a click** when the vulnerability is in KEV, its EPSS
  is at or above 0.1, its CVSS is at or above 7.0 (or its label is high or
  critical when no score is published), or the workload faces the internet, and
  nothing stands in the way of a plain bump.
- **An inbox item** otherwise, one per repository: "Open the pull request for
  4 findings in acme/api". Approving it opens the same pull request.

The thresholds live in the workspace config under `patch.act_on`; `patch.ignore`
silences an advisory until a date with a reason. The PR watcher's policy and the
access model's grants are the only other controls.

```json
"patch": {
  "act_on": {"kev": true, "epss_min": 0.1, "cvss_min": 7.0, "exposed": true},
  "repos": [],
  "max_open_prs": 10,
  "ignore": [{"advisory": "CVE-2026-109618", "until": "2026-12-01", "reason": "mitigated at the WAF"}]
}
```

## States

```
open ──(pull request)──▶ pr_open ──(merge)──▶ merged ──(the map shows the fix running)──▶ deployed
  │  (inbox, approved) ──▶ approved ──▶ pr_open …
  └──(a person, with a reason, until a date)──▶ ignored
any ──(the inventory no longer matches)──▶ closed
```

A finding closes itself when the inventory no longer matches; one whose pull
request merged becomes `deployed` when the next scan sees the fix running.

## The tab, the API, the commands

The Patches tab lists the findings with their severity, KEV and EPSS, the fix
version, the workloads and repository, the verdict and the state, with filters
for exposed, exploited and waiting for merge; its "Needs you" view is the inbox
for this tab. `GET /api/patches` is the same in one call; `POST
/api/patches/findings/approve`, `…/ignore`, `…/{key}/reopen` are a person's
decisions; `POST /api/patches/feed/run` and `/api/patches/tick` start the jobs.
`compass patch findings [state=…] [exposed=true] [exploited=true] [repo=…]`,
`compass patch tick`, `compass patch approve keys=[…]`, `compass patch ignore key=…
until=… reason=…`, `compass patch feed_status`, `compass patch inventory`.

## What it never does

It never patches a running image in place, never merges, and never touches a
site Compass cannot see: a host outside AWS, a repository the mapper does not
read. A container-image Lambda's image is not read (that needs
`lambda:GetFunction`, which the access model forbids); the node says
`packageType: Image` so the gap is visible.
