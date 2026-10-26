"""The feeds and the advisory store (docs/specs/patching-captain.md §3.2, §6): every source
parsed from a recorded fixture, the cursors advancing, a second run fetching nothing, one
source failing without taking the others down, and the network refused."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from captains.mapper import software as sw
from captains.patch import advisories as adv
from captains.patch import feed, inventory, sources
from captains.patch.store import AdvisoryStore
from compass_core.keys import workspace_pk
from infra_map.models import ResourceNode

PK = workspace_pk("ws-a")
NOW = datetime(2026, 10, 22, 15, 0, tzinfo=timezone.utc)
PHP2SHELL = "CVE-2026-109618"
LARAVEL_GHSA = "GHSA-aaaa-bbbb-cccc"
LARAVEL_CVE = "CVE-2026-20001"

# ── the recorded feeds ───────────────────────────────────────────────────────

OSV_LARAVEL = {
    "id": LARAVEL_GHSA, "aliases": [LARAVEL_CVE], "summary": "Laravel framework: query injection through unvalidated column names",
    "details": "A long description.", "published": "2026-10-20T10:00:00Z", "modified": "2026-10-21T09:00:00Z",
    "severity": [{"type": "CVSS_V3", "score": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"}],
    "affected": [{"package": {"ecosystem": "Packagist", "name": "laravel/framework", "purl": "pkg:composer/laravel/framework"},
                  "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "11.0.0"}, {"fixed": "11.2.1"}, {"introduced": "12.0.0"}, {"fixed": "12.0.3"}]}]}],
    "references": [{"type": "ADVISORY", "url": f"https://github.com/advisories/{LARAVEL_GHSA}"}],
    "database_specific": {"severity": "CRITICAL"},
}
OSV_OLD_EXPRESS = {"id": "GHSA-express-0001", "aliases": ["CVE-2024-29041"], "summary": "express: open redirect", "modified": "2024-03-25T00:00:00Z",
                   "affected": [{"package": {"ecosystem": "npm", "name": "express"}, "ranges": [{"type": "SEMVER", "events": [{"introduced": "0"}, {"fixed": "4.19.2"}]}]}]}

CVE_PHP2SHELL = {
    "cveMetadata": {"cveId": PHP2SHELL, "datePublished": "2026-10-21T14:00:00Z", "dateUpdated": "2026-10-22T13:40:00Z"},
    "containers": {
        "cna": {
            "title": "php2shell: memory exhaustion in the multipart parser leaves an executable file behind",
            "descriptions": [{"lang": "en", "value": "Crafted multipart requests exhaust memory_limit; what PHP leaves behind gives pre-auth RCE."}],
            "affected": [{"vendor": "PHP Group", "product": "PHP", "versions": [
                {"version": "8.2.0", "lessThan": "8.2.35", "status": "affected", "versionType": "semver"},
                {"version": "8.3.0", "lessThan": "8.3.36", "status": "affected", "versionType": "semver"},
                {"version": "8.4.0", "lessThan": "8.4.27", "status": "affected", "versionType": "semver"},
                {"version": "8.5.0", "lessThan": "8.5.12", "status": "affected", "versionType": "semver"}]}],
            "metrics": [{"cvssV3_1": {"baseScore": 9.2, "vectorString": "CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:C/C:H/I:H/A:L"}}],
            "references": [{"url": "https://github.com/php/php-src/security/advisories/GHSA-w8hw-w9fx-j79w"}, {"url": "https://calif.io/php2shell/"}],
        },
        "adp": [{"title": "CISA ADP Vulnrichment", "metrics": [{"other": {"type": "kev", "content": {"dateAdded": "2026-10-22", "dueDate": "2026-11-12"}}}]}],
    },
}
CVE_UNRELATED = {"cveMetadata": {"cveId": "CVE-2026-500000", "dateUpdated": "2026-10-22T13:00:00Z"},
                 "containers": {"cna": {"descriptions": [{"lang": "en", "value": "A router does a thing."}],
                                        "affected": [{"vendor": "ACME Routers", "product": "Router OS", "versions": [{"version": "1.0", "lessThan": "1.1", "status": "affected"}]}]}}}
DELTA_LOG = [
    {"fetchTime": "2026-10-22T14:00:00Z", "numberOfChanges": 2,
     "new": [{"cveId": PHP2SHELL, "githubLink": f"https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/109xxx/{PHP2SHELL}.json"}],
     "updated": [{"cveId": "CVE-2026-500000", "githubLink": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/500xxx/CVE-2026-500000.json"}]},
    {"fetchTime": "2026-10-20T10:00:00Z", "numberOfChanges": 0, "new": [], "updated": []},
]
KEV = {"catalogVersion": "2026.10.22", "vulnerabilities": [
    {"cveID": PHP2SHELL, "vendorProject": "PHP Group", "product": "PHP", "vulnerabilityName": "PHP multipart memory exhaustion", "dateAdded": "2026-10-22", "dueDate": "2026-11-12",
     "shortDescription": "Pre-auth RCE.", "knownRansomwareCampaignUse": "Unknown", "notes": "https://www.php.net/archive/2026.php ; https://nvd.nist.gov/vuln/detail/CVE-2026-109618"},
    {"cveID": "CVE-2021-44228", "vendorProject": "Apache", "product": "Log4j2", "vulnerabilityName": "Log4Shell", "dateAdded": "2021-12-10", "dueDate": "2021-12-24",
     "shortDescription": "JNDI.", "knownRansomwareCampaignUse": "Known"},
]}
EPSS = {"data": [{"cve": PHP2SHELL, "epss": "0.43120", "percentile": "0.97300", "date": "2026-10-22"}, {"cve": LARAVEL_CVE, "epss": "0.00210", "percentile": "0.55000", "date": "2026-10-22"}]}
UBUNTU = {"id": PHP2SHELL, "priority": "high", "notices": [{"id": "USN-8948-1"}],
          "packages": [{"name": "php8.3", "statuses": [{"release_codename": "noble", "status": "released", "description": "8.3.6-0ubuntu0.24.04.13"},
                                                       {"release_codename": "jammy", "status": "DNE", "description": ""}]},
                       {"name": "php8.1", "statuses": [{"release_codename": "jammy", "status": "needed", "description": ""}]}]}
DEBIAN = """<html><body><h1>CVE-2026-109618</h1>
<table><tr><th>Source Package</th><th>Release</th><th>Version</th><th>Status</th></tr>
<tr><td><a href="/tracker/source-package/php8.2">php8.2</a> (<a href="https://tracker.debian.org/php8.2">PTS</a>)</td><td>bookworm</td><td>8.2.28-1~deb12u1</td><td>vulnerable</td></tr>
<tr><td>php8.4</td><td>trixie</td><td>8.4.26-1</td><td>vulnerable</td></tr>
<tr><td>php8.4</td><td>sid</td><td>8.4.27-1</td><td>fixed</td></tr></table>
<p>The information below is based on the following data on fixed versions.</p>
<table><tr><th>Package</th><th>Type</th><th>Release</th><th>Fixed Version</th><th>Urgency</th><th>Origin</th><th>Debian Bugs</th></tr>
<tr><td>php8.2</td><td>source</td><td>bookworm (security)</td><td>8.2.35-1~deb12u1</td><td></td><td>DSA-6100-1</td><td></td></tr>
<tr><td>php8.2</td><td>source</td><td>(unstable)</td><td>(unfixed)</td><td></td><td></td><td></td></tr>
<tr><td>php8.4</td><td>source</td><td>trixie (security)</td><td>8.4.27-1~deb13u1</td><td></td><td>DSA-6101-1</td><td></td></tr>
<tr><td>php8.4</td><td>source</td><td>(unstable)</td><td>8.4.27-1</td><td></td><td></td><td></td></tr></table></body></html>"""
ALPINE_MAIN = {"packages": [{"pkg": {"name": "php83", "secfixes": {"8.3.36-r0": [PHP2SHELL, "CVE-2026-109619"], "8.3.12-r0": ["CVE-2024-11234"]}}},
                            {"pkg": {"name": "nginx", "secfixes": {"1.27.2-r0": ["CVE-2026-1"]}}}]}
ALAS = """<html><table><tr><th>Advisory</th><th>Severity</th><th>Packages</th><th>Release date</th></tr>
<tr><td><a href="/ALAS2023-2026-3302.html">ALAS2023-2026-3302</a></td><td>Critical</td><td>php8.3</td><td>2026-10-22</td></tr>
<tr><td><a href="/ALAS2PHP8.2-2026-015.html">ALAS2PHP8.2-2026-015</a></td><td>Critical</td><td>php</td><td>2026-10-22</td></tr></table></html>"""
PACKAGIST = {"advisories": {"laravel/framework": [{"advisoryId": "PKSA-abcd-1234", "packageName": "laravel/framework", "remoteId": LARAVEL_GHSA, "title": "Query injection",
                                                   "link": f"https://github.com/advisories/{LARAVEL_GHSA}", "cve": LARAVEL_CVE, "affectedVersions": ">=11.0.0,<11.2.1|>=12.0.0,<12.0.3",
                                                   "reportedAt": "2026-10-20 10:00:00", "sources": [{"name": "GitHub", "remoteId": LARAVEL_GHSA}]}],
                            "vendor/other": [{"advisoryId": "PKSA-zzzz", "packageName": "vendor/other", "title": "x", "affectedVersions": "<1.0", "reportedAt": "2026-10-20 10:00:00"}]}}
NPM_BULK = {"express": [{"id": 1096820, "url": "https://github.com/advisories/GHSA-rv95-896h-c2vc", "title": "Express.js Open Redirect", "severity": "moderate",
                         "vulnerable_versions": "<4.19.2", "cve": ["CVE-2024-29041"]}]}
OSV_CSV_PACKAGIST = f"{LARAVEL_GHSA},2026-10-21T09:00:00Z\nGHSA-old-0001,2025-01-01T00:00:00Z\n"


class FakeHttp:
    """Canned answers by URL (and query); anything else is a refused network call."""

    def __init__(self, answers):
        self.answers = dict(answers)
        self.calls = 0
        self.log = []

    def _key(self, url, params):
        return url + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "")

    def _answer(self, url, params, allow_404):
        self.calls += 1
        key = self._key(url, params)
        self.log.append(key)
        if key not in self.answers:
            for prefix, value in self.answers.items():
                if prefix.endswith("*") and key.startswith(prefix[:-1]):
                    return value
            if allow_404:
                return None
            raise AssertionError(f"network refused: {key}")
        value = self.answers[key]
        if isinstance(value, Exception):
            raise value
        return value

    def get(self, url, params=None, *, allow_404=False):
        value = self._answer(url, params, allow_404)
        return value if value is None or isinstance(value, str) else json.dumps(value)

    def get_json(self, url, params=None, *, allow_404=False):
        value = self._answer(url, params, allow_404)
        return json.loads(value) if isinstance(value, str) else value

    def post_json(self, url, body):
        self.calls += 1
        self.log.append(f"POST {url}")
        if url == f"{sources.OSV_API}/querybatch":
            results = []
            for query in body["queries"]:
                purl = query["package"]["purl"]
                vulns = []
                if purl.startswith("pkg:composer/laravel/framework"):
                    vulns.append({"id": LARAVEL_GHSA, "modified": OSV_LARAVEL["modified"]})
                if purl.startswith("pkg:npm/express"):
                    vulns.append({"id": "GHSA-express-0001", "modified": OSV_OLD_EXPRESS["modified"]})
                results.append({"vulns": vulns})
            return {"results": results}
        if url == sources.NPM_BULK_URL:
            assert "express" in body
            return NPM_BULK
        raise AssertionError(f"network refused: POST {url}")


def answers(**overrides):
    base = {
        f"{sources.OSV_API}/vulns/{LARAVEL_GHSA}": OSV_LARAVEL,
        f"{sources.OSV_API}/vulns/GHSA-express-0001": OSV_OLD_EXPRESS,
        f"{sources.OSV_DATA}/Packagist/modified_id.csv": OSV_CSV_PACKAGIST,
        f"{sources.OSV_DATA}/npm/modified_id.csv": "GHSA-express-0001,2024-03-25T00:00:00Z\n",
        f"{sources.OSV_DATA}/Ubuntu/modified_id.csv": "",
        f"{sources.OSV_DATA}/Debian/modified_id.csv": "",
        f"{sources.OSV_DATA}/Alpine/modified_id.csv": "",
        sources.CVELIST_DELTA: DELTA_LOG,
        f"https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/109xxx/{PHP2SHELL}.json": CVE_PHP2SHELL,
        "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/500xxx/CVE-2026-500000.json": CVE_UNRELATED,
        sources.KEV_URL: KEV,
        f"{sources.EPSS_URL}?cve=CVE-2024-29041,{PHP2SHELL},{LARAVEL_CVE}": EPSS,
        f"{sources.EPSS_URL}?cve={PHP2SHELL},{LARAVEL_CVE}": EPSS,
        sources.UBUNTU_URL.format(cve=PHP2SHELL): UBUNTU,
        sources.DEBIAN_URL.format(cve=PHP2SHELL): DEBIAN,
        sources.ALPINE_URL.format(release="v3.20", repo="main"): ALPINE_MAIN,
        sources.ALPINE_URL.format(release="v3.20", repo="community"): {"packages": []},
        sources.ALAS_URL.format(cve=PHP2SHELL): ALAS,
        f"{sources.PACKAGIST_URL}?updatedSince=*": PACKAGIST,
    }
    base.update(overrides)
    return base


# ── the map ──────────────────────────────────────────────────────────────────

def _node(rid, ntype, name, entries, **attributes):
    attrs = dict(attributes)
    sw.put_software(attrs, entries)
    return ResourceNode(org_id="o", resource_id=rid, type=ntype, account_id="1", region="us-east-1", name=name, attributes=attrs)


def world():
    web = sw.parse_dockerfile("FROM php:8.3.30-fpm\nRUN apt-get install -y libzip-dev\n", path="Dockerfile").software
    legacy = sw.parse_dockerfile("FROM ubuntu:24.04\nRUN apt-get install -y php8.3-fpm=8.3.6-0ubuntu0.24.04.12\n", path="Dockerfile").software
    alpine = sw.parse_dockerfile("FROM alpine:3.20\nRUN apk add php83=8.3.12-r0\n", path="Dockerfile").software
    lock = sw.parse_lockfile("composer.lock", json.dumps({"packages": [{"name": "laravel/framework", "version": "v11.2.0"}]}), {"laravel/framework"})
    npm_lock = sw.parse_lockfile("package-lock.json", json.dumps({"lockfileVersion": 3, "packages": {"": {}, "node_modules/express": {"version": "4.18.2"}}}))
    images, td_entries = sw.task_definition_software({"containerDefinitions": [{"name": "web", "image": "123456789012.dkr.ecr.us-east-1.amazonaws.com/acme-web:41"}]})
    return [
        _node("td-web", "ecs_task_definition", "web:41", td_entries, images=images, exposed=True),
        _node("file-web", "github_dockerfile", "web/Dockerfile", web, repo="acme/web", path="Dockerfile"),
        _node("file-legacy", "github_dockerfile", "legacy/Dockerfile", legacy, repo="acme/legacy", path="Dockerfile"),
        _node("file-alpine", "github_dockerfile", "alpine/Dockerfile", alpine, repo="acme/alpine", path="Dockerfile"),
        _node("file-lock", "github_lockfile", "web/composer.lock", lock, repo="acme/web", path="composer.lock"),
        _node("file-npm", "github_lockfile", "web/package-lock.json", npm_lock, repo="acme/web", path="package-lock.json"),
        _node("fn-1", "lambda_function", "api", sw.lambda_software({"Runtime": "provided.al2", "Layers": [{"Arn": "arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51"}]})),
        ResourceNode(org_id="o", resource_id="sg-1", type="ec2_security_group", account_id="1", region="us-east-1", name="sg", attributes={}),
    ]


@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        yield AdvisoryStore(table, PK)


def run(store, http, *, now=NOW, nodes=None, sources_=None):
    return feed.run({}, workspace_id="ws-a", http=http, now=now, inventory=inventory.collect(nodes if nodes is not None else world()), store=store, sources=sources_)


# ── the inventory ────────────────────────────────────────────────────────────

def test_the_inventory_reads_the_map_and_says_what_each_feed_should_be_asked():
    inv = inventory.collect(world())
    assert inv.nodes_read == 8 and inv.nodes_with_software == 7
    assert inv.osv_purls() == ["pkg:apk/alpine/php83@8.3.12-r0", "pkg:composer/laravel/framework@11.2.0", "pkg:deb/ubuntu/php8.3-fpm@8.3.6-0ubuntu0.24.04.12",
                               "pkg:deb/ubuntu/php8.3@8.3.6-0ubuntu0.24.04.12", "pkg:npm/express@4.18.2"], "a binary package is asked about by its source name too"
    assert inv.osv_ecosystems() == ["Alpine", "Debian", "Packagist", "Ubuntu", "npm"], "the php image is Debian underneath"
    assert inv.distro_releases() == {"ubuntu": {"24.04"}, "alpine": {"3.20"}, "debian": set()}
    assert inv.products() == {"php": {"8.3.30"}, "ubuntu": {"24.04"}, "alpine": {"3.20"}, "aws-lambda-runtime": {"provided.al2"}}
    assert inv.npm_packages() == {"express": {"4.18.2"}}
    exposed = [i for i in inv.items if i.exposed]
    assert exposed and all(i.node_id == "td-web" for i in exposed)
    summary = inv.summary()
    assert summary["exposed_workloads"] == 1 and summary["by_type"]["deb"] == 2 and summary["osv_purls"] == 5


# ── the first run ────────────────────────────────────────────────────────────

def test_the_first_run_is_current_in_one_pass_from_the_inventory_and_the_catalogues(store):
    http = FakeHttp(answers())
    report = run(store, http)
    assert not [n for n, s in report["sources"].items() if s.get("error")], report["sources"]
    assert report["sources"]["osv"]["queried_purls"] == 5 and report["sources"]["osv"]["fetched"] == 2
    assert report["sources"]["cvelist"]["fetched"] == 1 and report["sources"]["cvelist"]["stored"] == 1
    assert not any("CVE-2026-500000" in call for call in http.log), "an update to a CVE nobody here knows is NVD's enrichment, not read"
    assert report["sources"]["kev"]["stored"] == 1, "Log4Shell is not on the map; php2shell is"
    assert f"{sources.OSV_API}/vulns/{LARAVEL_GHSA}" in http.log and "POST https://api.osv.dev/v1/querybatch" in http.log
    assert not any("GHSA-old-0001" in call for call in http.log), "the first run does not backfill the ecosystem's history"

    php = store.get(PHP2SHELL)
    assert php.cvss == 9.2 and php.severity == "critical" and php.kev == {"added": "2026-10-22", "due": "2026-11-12", "ransomware": False}
    assert php.epss == {"score": 0.4312, "percentile": 0.973, "date": "2026-10-22"}
    assert php.summary.startswith("php2shell") and "https://calif.io/php2shell/" in php.references
    by_source = {}
    for entry in php.affected:
        by_source.setdefault(entry.source, []).append(entry)
    assert [(a.name, a.fixed) for a in by_source["cvelist"]] == [("php", ["8.2.35", "8.3.36", "8.4.27", "8.5.12"])]
    assert by_source["cvelist"][0].product == "php" and by_source["cvelist"][0].ranges[1].introduced == "8.3.0"
    ubuntu = {(a.name, a.release): (a.status, a.fixed, a.advisory_id) for a in by_source["ubuntu"]}
    assert ubuntu == {("php8.3", "24.04"): ("released", ["8.3.6-0ubuntu0.24.04.13"], "USN-8948-1"), ("php8.1", "22.04"): ("needed", [], "USN-8948-1")}, "DNE rows are dropped"
    debian = {(a.name, a.release): (a.status, a.fixed) for a in by_source["debian"]}
    assert debian == {("php8.2", "bookworm"): ("vulnerable", ["8.2.35-1~deb12u1"]), ("php8.4", "trixie"): ("vulnerable", ["8.4.27-1~deb13u1"]), ("php8.4", "sid"): ("fixed", ["8.4.27-1"])}
    assert [(a.name, a.release, a.fixed) for a in by_source["alpine"]] == [("php83", "3.20", ["8.3.36-r0"])]
    assert [(a.name, a.release, a.fixed, a.advisory_id) for a in by_source["alas"]] == [("php8.3", "2023", [], "ALAS2023-2026-3302"), ("php", "2", [], "ALAS2PHP8.2-2026-015")]
    assert set(php.sources) == {"cvelist", "kev", "epss", "ubuntu", "debian", "alpine", "alas"}

    laravel = store.get(LARAVEL_CVE)
    assert laravel is not None and store.get(LARAVEL_GHSA) is laravel or store.get(LARAVEL_GHSA).id == LARAVEL_CVE, "the GHSA is an alias of the CVE"
    assert set(laravel.aliases) == {LARAVEL_GHSA, "PKSA-abcd-1234"} and laravel.cvss == 9.8 and laravel.epss["score"] == 0.0021
    osv_entry = next(a for a in laravel.affected if a.source == "osv")
    assert osv_entry.purl_type == "composer" and osv_entry.fixed == ["11.2.1", "12.0.3"] and [r.introduced for r in osv_entry.ranges] == ["11.0.0", "12.0.0"]
    packagist_entry = next(a for a in laravel.affected if a.source == "packagist")
    assert packagist_entry.fixed == ["11.2.1", "12.0.3"] and packagist_entry.advisory_id == "PKSA-abcd-1234"
    assert store.get("PKSA-zzzz") is None, "a package not on the map is not stored"
    express = store.get("CVE-2024-29041")
    assert express is not None and {a.source for a in express.affected} == {"osv", "npm"} and express.affected[-1].fixed == ["4.19.2"]

    assert store.count() == 3 and report["advisories"] == 3
    cursors = store.cursors()
    assert cursors["kev"]["value"] == "2026.10.22" and PHP2SHELL in cursors["kev"]["ids"]
    assert cursors["osv"]["value"] == {"Alpine": "2026-10-22T15:00:00Z", "Debian": "2026-10-22T15:00:00Z", "Packagist": "2026-10-21T09:00:00Z",
                                       "Ubuntu": "2026-10-22T15:00:00Z", "npm": "2024-03-25T00:00:00Z"}
    assert cursors["cvelist"]["value"] == "2026-10-22T14:00:00Z" and cursors["epss"]["value"] == "2026-10-22"
    status = feed.status({}, workspace_id="ws-a", store=store)
    assert status["advisories"] == 3 and status["last_run"] == report["finished_at"] and status["sources"]["kev"]["last_success"] == report["started_at"]
    assert status["inventory"]["osv_purls"] == 5


def test_a_second_run_the_same_day_fetches_nothing_and_an_hour_later_only_what_changed(store):
    run(store, FakeHttp(answers()))
    quiet = FakeHttp(answers())
    report = run(store, quiet, now=NOW.replace(hour=16))
    assert not [n for n, s in report["sources"].items() if s.get("error")], report["sources"]
    assert not any("/v1/vulns/" in call or "raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026" in call for call in quiet.log), quiet.log
    assert report["sources"]["kev"]["stored"] == 0 and report["sources"]["epss"]["fetched"] == 0 and report["sources"]["ubuntu"]["fetched"] == 0
    assert report["sources"]["alpine"]["fetched"] == 0 and report["sources"]["npm"]["fetched"] == 0
    assert store.count() == 3
    # the advisory changes upstream: the delta names it, it is fetched again
    newer = dict(OSV_LARAVEL, modified="2026-10-22T15:30:00Z", summary="Laravel framework: query injection (updated)")
    changed = FakeHttp(answers(**{f"{sources.OSV_DATA}/Packagist/modified_id.csv": f"{LARAVEL_GHSA},2026-10-22T15:30:00Z\n", f"{sources.OSV_API}/vulns/{LARAVEL_GHSA}": newer}))
    changed.answers[f"{sources.OSV_API}/vulns/{LARAVEL_GHSA}"] = newer
    report = run(store, changed, now=NOW.replace(hour=17), sources_=["osv"])
    assert report["sources"]["osv"]["fetched"] == 1 and report["sources"]["osv"]["delta_ids"] == 1
    assert store.get(LARAVEL_CVE).summary.endswith("(updated)")


def test_one_source_failing_is_reported_and_the_others_run(store):
    http = FakeHttp(answers(**{sources.DEBIAN_URL.format(cve=PHP2SHELL): RuntimeError("502 Bad Gateway"), sources.KEV_URL: RuntimeError("timeout")}))
    report = run(store, http)
    assert report["sources"]["kev"]["error"].startswith("RuntimeError: timeout")
    assert report["sources"]["cvelist"]["stored"] == 1 and report["sources"]["ubuntu"]["stored"] == 1
    php = store.get(PHP2SHELL)
    assert php.kev == {"added": "2026-10-22", "due": "2026-11-12"}, "the CVE record's ADP container says KEV even when the catalogue is down"
    # a tracker's page that fails is skipped, not stamped: the CVE is asked about again next run
    assert "error" not in report["sources"]["debian"] and report["sources"]["debian"]["stored"] == 0
    assert "debian" not in {a.source for a in php.affected} and "debian" not in php.sources and "ubuntu" in php.sources
    assert "kev" not in store.cursors()
    again = run(store, FakeHttp(answers()), now=NOW.replace(hour=16), sources_=["debian"])
    assert again["sources"]["debian"]["stored"] == 1 and "debian" in store.get(PHP2SHELL).sources


def test_the_cve_delta_cursor_stays_on_the_last_entry_read_whole_and_old_churn_is_skipped(store, monkeypatch):
    monkeypatch.setattr(sources, "MAX_CVE_RECORDS", 1)
    log = [
        {"fetchTime": "2026-10-22T13:00:00Z", "new": [{"cveId": "CVE-2026-500000", "githubLink": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/500xxx/CVE-2026-500000.json"}],
         "updated": [{"cveId": "CVE-2026-400000", "githubLink": "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/400xxx/CVE-2026-400000.json"}]},
        {"fetchTime": "2026-10-22T14:00:00Z", "new": [{"cveId": PHP2SHELL, "githubLink": f"https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/109xxx/{PHP2SHELL}.json"}], "updated": []},
    ]
    http = FakeHttp(answers(**{sources.CVELIST_DELTA: log}))
    first = run(store, http, sources_=["cvelist"])
    assert first["sources"]["cvelist"]["fetched"] == 1 and first["sources"]["cvelist"]["cursor"] == "2026-10-22T13:00:00Z"
    assert first["sources"]["cvelist"]["note"].startswith("deltas after 2026-10-22T13:00:00Z wait")
    assert not any("CVE-2026-400000" in call for call in http.log), "an update to a CVE nobody here knows is enrichment churn"
    second = run(store, http, now=NOW.replace(hour=16), sources_=["cvelist"])
    assert second["sources"]["cvelist"]["fetched"] == 1 and second["sources"]["cvelist"]["cursor"] == "2026-10-22T14:00:00Z" and store.get(PHP2SHELL) is not None
    third = run(store, http, now=NOW.replace(hour=17), sources_=["cvelist"])
    assert third["sources"]["cvelist"]["fetched"] == 0


def test_the_network_is_refused_for_anything_the_fixtures_do_not_answer(store):
    http = FakeHttp({sources.KEV_URL: KEV})
    report = run(store, http, sources_=["kev", "cvelist"])
    assert report["sources"]["kev"]["stored"] == 1
    assert report["sources"]["cvelist"]["error"].startswith("AssertionError: network refused: https://raw.githubusercontent.com")


def test_inject_is_a_test_only_door_and_takes_an_osv_record_or_the_stores_own_shape(store, monkeypatch):
    monkeypatch.delenv("COMPASS_PATCH_INJECT", raising=False)
    with pytest.raises(ValueError, match="COMPASS_PATCH_INJECT"):
        feed.inject({}, workspace_id="ws-a", advisory=OSV_LARAVEL, store=store)
    monkeypatch.setenv("COMPASS_PATCH_INJECT", "1")
    first = feed.inject({}, workspace_id="ws-a", advisory=OSV_LARAVEL, store=store)
    assert first["id"] == LARAVEL_CVE and first["cvss"] == 9.8 and first["affected"] == 1
    own = {"id": PHP2SHELL, "summary": "php2shell", "cvss": 9.2, "kev": {"added": "2026-10-22"},
           "affected": [{"purl_type": "docker", "name": "php", "source": "inject", "product": "php", "ranges": [{"introduced": "8.3.0", "fixed": "8.3.36"}], "fixed": ["8.3.36"]}]}
    second = feed.inject({}, workspace_id="ws-a", advisory=own, store=store)
    assert second == {"id": PHP2SHELL, "aliases": [], "affected": 1, "cvss": 9.2, "kev": True}
    assert store.get(PHP2SHELL).affected[0].product == "php" and store.count() == 2
    assert {a["id"] for a in feed.advisories({}, workspace_id="ws-a", store=store)} == {LARAVEL_CVE, PHP2SHELL}


# ── the shared parsers ───────────────────────────────────────────────────────

def test_an_osv_record_with_only_a_cvss_v4_vector_keeps_the_vector_and_the_label():
    record = dict(OSV_LARAVEL, severity=[{"type": "CVSS_V4", "score": "CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:N/VI:H/VA:N/SC:N/SI:N/SA:N"}], database_specific={"severity": "HIGH"})
    parsed = sources.parse_osv(record)
    assert parsed.cvss is None and parsed.cvss_vector.startswith("CVSS:4.0") and parsed.severity == "high"
    assert sources.parse_cve_record({"cveMetadata": {"cveId": "CVE-2026-1"}, "containers": {"cna": {"affected": [
        {"vendor": "PHP Group", "product": "PHP", "versions": [{"version": "8.3.*", "lessThan": "8.3.36", "status": "affected", "versionType": "semver"}]}]}}}
    ).affected[0].ranges[0].introduced == "8.3.0", "a series wildcard starts at .0"


def test_cvss_vectors_score_as_the_specification_does():
    assert adv.cvss_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H") == 9.8
    assert adv.cvss_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:R/S:C/C:L/I:L/A:N") == 6.1
    assert adv.cvss_score("CVSS:3.1/AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:H/A:H") == 9.9
    assert adv.cvss_score("CVSS:3.0/AV:L/AC:H/PR:H/UI:R/S:U/C:L/I:N/A:N") == 1.8
    assert adv.cvss_score("CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:N/I:N/A:N") == 0.0
    assert adv.cvss_score("CVSS:4.0/AV:N/AC:L/AT:N/PR:N/UI:N/VC:H/VI:H/VA:H/SC:N/SI:N/SA:N") is None
    assert adv.severity_label(9.2) == "critical" and adv.severity_label(7.0) == "high" and adv.severity_label(3.9) == "low"


def test_constraints_become_ranges_and_ids_merge_on_the_cve():
    ranges = adv.ranges_from_constraint(">=11.0.0,<11.2.1|>=12.0.0,<12.0.3")
    assert [(r.introduced, r.fixed) for r in ranges] == [("11.0.0", "11.2.1"), ("12.0.0", "12.0.3")]
    assert [(r.introduced, r.fixed, r.last_affected) for r in adv.ranges_from_constraint("<4.19.2")] == [("0", "4.19.2", None)]
    assert [(r.introduced, r.last_affected) for r in adv.ranges_from_constraint(">=1.0.0 <=1.2.3 || =2.0.0")] == [("1.0.0", "1.2.3"), ("2.0.0", "2.0.0")]
    assert adv.canonical_id([LARAVEL_GHSA, LARAVEL_CVE]) == LARAVEL_CVE and adv.canonical_id(["PKSA-1"]) == "PKSA-1"
    first = adv.merge(None, adv.Advisory(id=LARAVEL_GHSA, aliases=[LARAVEL_CVE], cvss=9.8, affected=[adv.Affected(purl_type="composer", name="laravel/framework", source="osv")],
                                         sources={"osv": "a"}))
    assert first.id == LARAVEL_CVE and first.aliases == [LARAVEL_GHSA] and first.severity == "critical"
    second = adv.merge(first, adv.Advisory(id="PKSA-1", aliases=[LARAVEL_GHSA], summary="Query injection", cvss=7.5,
                                           affected=[adv.Affected(purl_type="composer", name="laravel/framework", source="packagist")], sources={"packagist": "b"}))
    assert second.id == LARAVEL_CVE and set(second.aliases) == {LARAVEL_GHSA, "PKSA-1"} and second.cvss == 9.8 and second.summary == "Query injection"
    assert [a.source for a in second.affected] == ["osv", "packagist"] and set(second.sources) == {"osv", "packagist"}
    third = adv.merge(second, adv.Advisory(id=LARAVEL_CVE, affected=[adv.Affected(purl_type="composer", name="laravel/framework", source="osv", fixed=["11.2.1"])], sources={"osv": "c"}))
    assert [(a.source, a.fixed) for a in third.affected] == [("packagist", []), ("osv", ["11.2.1"])], "a feed replaces its own entries and leaves the others'"
    assert adv.product_image("PHP Group", "PHP") == "php" and adv.product_image("F5", "NGINX Open Source") == "nginx" and adv.product_image("ACME", "Router OS") is None
    assert adv.product_image("Node.js", "Node") == "node" and adv.product_image("Python Software Foundation", "CPython") == "python"
