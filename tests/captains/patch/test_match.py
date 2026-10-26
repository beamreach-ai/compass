"""Versions, the match, the policy and the findings' states (docs/specs/patching-captain.md §3.3,
§3.4, §3.7, §6): php2shell against the inventory of §3.11 yields the three findings with the right
fixed versions; a transitive, a major-only fix and a floating tag wait in the inbox; each policy
trigger alone; an ignored advisory; a finding closes when the inventory changes."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from captains.mapper import software as sw
from captains.patch import findings as _findings
from captains.patch import inventory as _inventory
from captains.patch import match as _match
from captains.patch import policy, versions
from captains.patch.advisories import Advisory, Affected, Range
from compass_core.keys import workspace_pk
from infra_map.models import RelationshipEdge, ResourceNode

PK = workspace_pk("ws-a")
PHP2SHELL = "CVE-2026-109618"
ACCOUNT = "123456789012"
ECR = f"{ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com"
BREF = "arn:aws:lambda:us-east-1:534081306603:layer:php-83-fpm:51"


# ── versions ──────────────────────────────────────────────────────────────────

def test_versions_order_as_the_package_managers_do():
    assert versions.compare("deb", "8.3.6-0ubuntu0.24.04.12", "8.3.6-0ubuntu0.24.04.13") < 0
    assert versions.compare("deb", "1:8.2.35-1~deb12u1", "8.2.35-1~deb12u1") > 0, "an epoch wins"
    assert versions.compare("deb", "8.2.35-1~deb12u1", "8.2.35-1") < 0, "a tilde sorts before the end of the string"
    assert versions.compare("deb", "8.2.35-1", "8.2.35-1+b1") < 0 and versions.compare("deb", "2.0", "10.0") < 0
    assert versions.compare("apk", "8.3.12-r0", "8.3.36-r0") < 0 and versions.compare("apk", "8.3.36-r0", "8.3.36-r1") < 0
    assert versions.compare("rpm", "8.3.30-1.amzn2023.0.1", "8.3.36-1.amzn2023.0.1") < 0
    assert versions.compare("rpm", "8.3.36-1.amzn2023.0.1", "8.3.36-1.amzn2023.0.1") == 0 and versions.compare("rpm", "1.0~rc1", "1.0") < 0
    assert versions.compare("pypi", "2.31.0", "2.32.0rc1") < 0 and versions.compare("pypi", "5.0.6", "5.0.10") < 0
    assert versions.compare("composer", "v11.2.0", "11.2.1") < 0 and versions.compare("composer", "11.2.1-RC1", "11.2.1") < 0
    assert versions.compare("npm", "4.19.2", "4.20.0") < 0 and versions.compare("npm", "5.0.0-beta.3", "5.0.0") < 0
    assert versions.compare("golang", "v1.30.3", "v1.30.10") < 0 and versions.compare("docker", "8.3.30", "8.3.36") < 0
    rng = Range(introduced="8.3.0", fixed="8.3.36")
    assert versions.affected("docker", "8.3.30", [rng]) and not versions.affected("docker", "8.3.36", [rng]) and not versions.affected("docker", "8.2.9", [rng])
    assert versions.affected("npm", "4.17.1", [Range(introduced="0", fixed="4.19.2")]) and versions.affected("composer", "1.2.3", [], ["1.2.3"])
    assert versions.choose_fix("docker", "8.3.30", ["8.2.35", "8.3.36", "8.4.27"]) == ("8.3.36", False)
    assert versions.choose_fix("composer", "11.2.0", ["12.0.3"]) == ("12.0.3", True), "the only fix is a major upgrade"
    assert versions.choose_fix("composer", "11.2.0", ["11.2.1", "12.0.3"]) == ("11.2.1", False)
    assert versions.choose_fix("deb", None, ["8.3.6-0ubuntu0.24.04.13"]) == ("8.3.6-0ubuntu0.24.04.13", False)
    assert versions.choose_fix("docker", "8.3.36", ["8.3.36"]) == (None, False)
    assert versions.tag_version("8.3.30-fpm") == ("8.3.30", False) and versions.tag_version("8.3-fpm") == ("8.3", True) and versions.tag_version("latest") == (None, True)
    assert versions.fixed_tag("8.3.30-fpm-alpine3.20", "8.3.36") == "8.3.36-fpm-alpine3.20"


# ── the world of §3.11 ────────────────────────────────────────────────────────

def _node(rid, ntype, name, entries=(), **attributes):
    attrs = dict(attributes)
    sw.put_software(attrs, list(entries))
    return ResourceNode(org_id="o", resource_id=rid, type=ntype, account_id=ACCOUNT, region="us-east-1", name=name, attributes=attrs)


def _edge(src, dst, rel):
    return RelationshipEdge(org_id="o", from_resource_id=src, to_resource_id=dst, relationship_type=rel, confidence="high", evidence=[])


def php_shop(*, web_tag="8.3.30-fpm", legacy_pin="=8.3.6-0ubuntu0.24.04.12", exposed=True):
    """acme/web builds php:<tag> into ECR acme-web, run by task definition web:41 behind an internet-facing ALB;
    acme/legacy installs php8.3-fpm on ubuntu:24.04; acme/functions runs three Bref functions, two exposed."""
    images, td_entries = sw.task_definition_software({"containerDefinitions": [{"name": "web", "image": f"{ECR}/acme-web:41"}]}, lambda *a: "sha256:" + "a" * 64)
    web = sw.parse_dockerfile(f"FROM php:{web_tag}\nRUN apt-get update && apt-get install -y libzip-dev\n", path="Dockerfile").software
    legacy = sw.parse_dockerfile(f"FROM ubuntu:24.04\nRUN apt-get install -y php8.3-fpm{legacy_pin} nginx\n", path="Dockerfile").software
    lock = sw.parse_lockfile("composer.lock", json.dumps({"packages": [{"name": "laravel/framework", "version": "v11.2.0"}, {"name": "symfony/http-foundation", "version": "v6.4.0"}]}), {"laravel/framework"})
    fns = [_node(f"fn-{i}", "lambda_function", f"fn-{i}", sw.lambda_software({"Runtime": "provided.al2", "Layers": [{"Arn": BREF}]}), exposed=i < 2) for i in range(3)]
    nodes = [
        _node("td-web", "ecs_task_definition", "web:41", td_entries, images=images, exposed=exposed),
        _node("ecr-web", "ecr_repository", "acme-web", repositoryUri=f"{ECR}/acme-web"),
        _node("wf-web", "github_workflow", "build", repo="acme/web", events=["pull_request", "push"], jobs=["build"]),
        _node("f-web", "github_dockerfile", "web/Dockerfile", web, repo="acme/web", path="Dockerfile"),
        _node("f-web-lock", "github_lockfile", "web/composer.lock", lock, repo="acme/web", path="composer.lock"),
        _node("f-legacy", "github_dockerfile", "legacy/Dockerfile", legacy, repo="acme/legacy", path="Dockerfile"),
        _node("f-fns", "github_deploy_file", "functions/serverless.yml", sw.parse_deploy("serverless.yml", f"provider:\n  runtime: provided.al2\nfunctions:\n  web:\n    handler: index.php\n    layers:\n      - {BREF}\n"), repo="acme/functions", path="serverless.yml"),
        *fns,
    ]
    edges = [_edge("wf-web", "ecr-web", "publishes_image_to")]
    return nodes, edges


def php2shell(*, kev=False, epss=None, cvss=9.2):
    return Advisory(id=PHP2SHELL, summary="php2shell", cvss=cvss, severity="critical" if cvss and cvss >= 9 else None, kev={"added": "2026-10-22"} if kev else None,
                    epss={"score": epss} if epss is not None else None, affected=[
                        Affected(purl_type="docker", name="php", source="cvelist", product="php", ranges=[Range(introduced="8.2.0", fixed="8.2.35"), Range(introduced="8.3.0", fixed="8.3.36"), Range(introduced="8.4.0", fixed="8.4.27")],
                                 fixed=["8.2.35", "8.3.36", "8.4.27"]),
                        Affected(purl_type="deb", name="php8.3", source="ubuntu", namespace="ubuntu", release="24.04", status="released", fixed=["8.3.6-0ubuntu0.24.04.13"], advisory_id="USN-8948-1"),
                        Affected(purl_type="deb", name="php8.1", source="ubuntu", namespace="ubuntu", release="22.04", status="needed"),
                        Affected(purl_type="deb", name="php8.2", source="debian", namespace="debian", release="bookworm", status="fixed", fixed=["8.2.35-1~deb12u1"]),
                        Affected(purl_type="apk", name="php83", source="alpine", namespace="alpine", release="3.20", status="fixed", fixed=["8.3.36-r0"]),
                    ])


def laravel(fixed=("11.2.1", "12.0.3"), advisory_id="CVE-2026-20001"):
    return Advisory(id=advisory_id, aliases=["GHSA-aaaa-bbbb-cccc"], summary="Laravel query injection", cvss=9.8, severity="critical",
                    affected=[Affected(purl_type="composer", name="laravel/framework", source="osv", ranges=[Range(introduced="11.0.0", fixed=fixed[0])] + ([Range(introduced="12.0.0", fixed=fixed[1])] if len(fixed) > 1 else []),
                                       fixed=list(fixed))])


def run_match(nodes, edges, advisories):
    inv = _inventory.collect(nodes, edges)
    return _match.match(inv, advisories, _match.Topology.from_graph(nodes, edges))


def test_php2shell_against_the_php_shop_yields_the_three_findings_of_the_worked_example():
    nodes, edges = php_shop()
    found = run_match(nodes, edges, [php2shell()])
    by_node = {f.node_id: f for f in found}
    assert set(by_node) == {"f-web", "f-legacy", "fn-0", "fn-1", "fn-2", "f-fns"}, [f.id for f in found]
    web = by_node["f-web"]
    assert web.purl == "pkg:docker/php@8.3.30-fpm?final=true&stage=0" and web.version == "8.3.30" and web.fixed_version == "8.3.36" and web.blockers == []
    assert [(w.name, w.exposed, w.via) for w in web.workloads] == [("web:41", True, "image acme-web")] and web.exposed is True
    assert web.repo == "acme/web" and web.path == "Dockerfile" and web.evidence == "Dockerfile:1" and web.cvss == 9.2
    legacy = by_node["f-legacy"]
    assert legacy.package == "php8.3" and legacy.purl.startswith("pkg:deb/ubuntu/php8.3-fpm@8.3.6-0ubuntu0.24.04.12")
    assert legacy.fixed_version == "8.3.6-0ubuntu0.24.04.13" and legacy.release == "24.04" and legacy.blockers == [] and legacy.workloads == [] and legacy.exposed is False
    assert "nginx" not in {f.package for f in found}
    bref = by_node["fn-0"]
    assert bref.purl.startswith("pkg:generic/bref-layer/php-83-fpm@51") and bref.version == "8.3" and bref.fixed_version == "8.3.36"
    assert bref.blockers == ["unknown_version"] and bref.exposed is True and "Bref layer" in bref.note
    assert by_node["fn-2"].exposed is False and by_node["f-fns"].repo == "acme/functions"
    assert found[0].exposed and found[-1].exposed is False, "exposed first"


def test_the_fixed_image_is_not_a_finding_and_an_unpinned_package_on_a_fixed_release_is():
    nodes, edges = php_shop(web_tag="8.3.36-fpm", legacy_pin="")
    found = run_match(nodes, edges, [php2shell()])
    assert "f-web" not in {f.node_id for f in found}
    legacy = next(f for f in found if f.node_id == "f-legacy")
    assert legacy.version is None and legacy.fixed_version == "8.3.6-0ubuntu0.24.04.13" and legacy.blockers == [], "unpinned: the fix is the pin"


def test_a_floating_tag_a_transitive_a_major_only_fix_and_no_fix_are_said_as_blockers():
    nodes, edges = php_shop(web_tag="8.3-fpm")
    found = run_match(nodes, edges, [php2shell(), laravel(), laravel(fixed=("12.0.3",), advisory_id="CVE-2026-20002")])
    web = next(f for f in found if f.node_id == "f-web")
    assert web.blockers == ["floating_tag"] and web.version == "8.3" and "floating tag" in web.note
    lock = [f for f in found if f.node_id == "f-web-lock"]
    assert {(f.package, tuple(f.blockers), f.fixed_version) for f in lock} == {("laravel/framework", (), "11.2.1"), ("laravel/framework", ("major_upgrade",), "12.0.3")}
    transitive = Advisory(id="CVE-2026-30001", cvss=7.5, affected=[Affected(purl_type="composer", name="symfony/http-foundation", source="osv", ranges=[Range(introduced="6.0.0", fixed="6.4.1")], fixed=["6.4.1"])])
    [sym] = [f for f in run_match(nodes, edges, [transitive]) if f.package == "symfony/http-foundation"]
    assert sym.blockers == ["transitive"] and sym.relation == "transitive"
    no_fix = Advisory(id="CVE-2026-40001", cvss=8.0, affected=[Affected(purl_type="deb", name="php8.3", source="ubuntu", namespace="ubuntu", release="24.04", status="needed")])
    [needed] = run_match(nodes, edges, [no_fix])
    assert needed.node_id == "f-legacy" and needed.blockers == ["no_fix"] and needed.fixed_version is None
    withdrawn = Advisory(id="CVE-2026-50001", withdrawn="2026-10-01", affected=[Affected(purl_type="composer", name="laravel/framework", source="osv", ranges=[Range(introduced="0", fixed="99")])])
    assert run_match(nodes, edges, [withdrawn]) == []


def test_the_policy_opens_a_pull_request_on_each_trigger_alone_and_sends_the_rest_to_the_inbox():
    nodes, edges = php_shop(exposed=False)
    quiet = {"patch": {"act_on": {"kev": True, "epss_min": 0.1, "cvss_min": 7.0, "exposed": True}}}
    web = lambda adv: next(f for f in run_match(nodes, edges, [adv]) if f.node_id == "f-web")  # noqa: E731
    assert policy.decide(web(php2shell(cvss=5.0)), quiet) == ("inbox", ["below every trigger: not exploited, not exposed, not severe"])
    assert policy.decide(web(php2shell(cvss=5.0, kev=True)), quiet)[0] == "pr"
    assert policy.decide(web(php2shell(cvss=5.0, epss=0.12)), quiet) == ("pr", ["EPSS 0.12 ≥ 0.10"])
    assert policy.decide(web(php2shell(cvss=7.0)), quiet) == ("pr", ["CVSS 7.0 ≥ 7.0"])
    labelled = php2shell(cvss=None)
    labelled.severity = "high"
    assert policy.decide(web(labelled), quiet) == ("pr", ["severity high (no score published)"])
    exposed_nodes, exposed_edges = php_shop(exposed=True)
    verdict, reasons = policy.decide(next(f for f in run_match(exposed_nodes, exposed_edges, [php2shell(cvss=5.0)]) if f.node_id == "f-web"), quiet)
    assert verdict == "pr" and reasons == ["the workload faces the internet"]
    assert policy.decide(web(php2shell(cvss=5.0)), {"patch": {"act_on": {"cvss_min": 4.0}}}) == ("pr", ["CVSS 5.0 ≥ 4.0"])
    floating_nodes, floating_edges = php_shop(web_tag="8.3-fpm")
    verdict, reasons = policy.decide(next(f for f in run_match(floating_nodes, floating_edges, [php2shell()]) if f.node_id == "f-web"), quiet)
    assert verdict == "inbox" and reasons[-1].startswith("waits: a floating image tag")
    assert policy.ignored_until({"patch": {"ignore": [{"advisory": PHP2SHELL, "until": "2099-01-01", "reason": "mitigated at the WAF"}]}}, PHP2SHELL) == {"until": "2099-01-01", "reason": "mitigated at the WAF"}
    assert policy.ignored_until({"patch": {"ignore": [{"advisory": PHP2SHELL, "until": "2020-01-01"}]}}, PHP2SHELL) is None, "an ignore that ran out"


# ── the rows ─────────────────────────────────────────────────────────────────

@pytest.fixture
def store(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "test")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "test")
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="items", KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}], BillingMode="PAY_PER_REQUEST")
        yield _findings.FindingStore(table, PK)


def test_findings_open_refresh_close_deploy_and_remember_a_persons_decisions(store):
    nodes, edges = php_shop()
    config = {"patch": {}}
    first = store.sync(run_match(nodes, edges, [php2shell()]), config, now_ms=1000)
    assert first == {"opened": 6, "refreshed": 0, "reopened": 0, "closed": 0, "deployed": 0, "total": 6, "deployed_rows": []}
    rows = {r["node_id"]: r for r in store.list()}
    web, legacy, fn2 = rows["f-web"], rows["f-legacy"], rows["fn-2"]
    assert web["state"] == "open" and web["verdict"] == "pr" and web["first_seen"] == 1000 and "faces the internet" in " ".join(web["verdict_reasons"])
    assert legacy["verdict"] == "pr" and legacy["verdict_reasons"] == ["CVSS 9.2 ≥ 7.0"], "severe enough even unexposed"
    assert fn2["verdict"] == "inbox" and fn2["verdict_reasons"][-1].startswith("waits: the running version")
    summary = _findings.summary(store.list())
    assert summary["open"] == 6 and summary["exposed"] == 3 and summary["pr_now"] == 2 and summary["waiting_approval"] == 4 and summary["by_repo"]["acme/web"] == 1
    assert {r["node_id"] for r in store.list() if r["verdict"] == "pr"} == {"f-web", "f-legacy"}, "the Bref findings wait: which patch release the layer ships is not known"
    # a person approves one and ignores another; the next sync keeps both decisions and the first_seen
    assert store.approve(fn2["key"], by="ilia")["state"] == "approved"
    assert store.ignore(legacy["key"], by="ilia", until="2026-12-01", reason="behind the WAF")["state"] == "ignored"
    second = store.sync(run_match(nodes, edges, [php2shell(kev=True)]), config, now_ms=2000)
    assert second["refreshed"] == 6 and second["opened"] == 0
    rows = {r["node_id"]: r for r in store.list()}
    assert rows["fn-2"]["state"] == "approved" and rows["fn-2"]["approved_by"] == "ilia" and rows["f-legacy"]["state"] == "ignored" and rows["f-legacy"]["ignored"]["reason"] == "behind the WAF"
    assert rows["f-web"]["first_seen"] == 1000 and rows["f-web"]["last_seen"] == 2000 and rows["f-web"]["kev"] is True and "KEV" in rows["f-web"]["verdict_reasons"][0]
    # the pipeline marks the web fix merged; the rebuilt image leaves the map: deployed. The legacy fix lands too: closed.
    store.set_pr(rows["f-web"]["key"], state="merged", pr={"url": "https://github.com/acme/web/pull/9", "number": 9})
    fixed_nodes, fixed_edges = php_shop(web_tag="8.3.36-fpm", legacy_pin="=8.3.6-0ubuntu0.24.04.13")
    third = store.sync(run_match(fixed_nodes, fixed_edges, [php2shell(kev=True)]), config, now_ms=3000)
    assert third["deployed"] == 1 and third["closed"] == 1 and third["refreshed"] == 4
    rows = {r["node_id"]: r for r in store.list()}
    assert rows["f-web"]["state"] == "deployed" and rows["f-web"]["deployed_at"] == 3000 and rows["f-web"]["pr"]["number"] == 9
    assert rows["f-legacy"]["state"] == "closed" and rows["f-legacy"]["closed_at"] == 3000
    # it comes back: reopened, not duplicated
    fourth = store.sync(run_match(nodes, edges, [php2shell(kev=True)]), config, now_ms=4000)
    assert fourth["reopened"] == 2 and fourth["opened"] == 0 and len(store.list()) == 6
    # an ignore in the config opens the finding as ignored
    config_ignore = {"patch": {"ignore": [{"advisory": "CVE-2026-20001", "until": "2099-01-01", "reason": "not reachable"}]}}
    store.sync(run_match(nodes, edges, [php2shell(kev=True), laravel()]), config_ignore, now_ms=5000)
    lar = next(r for r in store.list() if r["advisory_id"] == "CVE-2026-20001")
    assert lar["state"] == "ignored" and lar["ignored"]["by"] == "config" and lar["ignored"]["reason"] == "not reachable"
    assert _findings.summary(store.list())["ignored"] == 1
    assert _match.coverage(nodes)["line"] == "1 of 1 images resolved" and _match.coverage(nodes)["software_files"] == 4
