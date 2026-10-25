"""The feeds (docs/specs/patching-captain.md §3.2): each one a function of the run's context
that returns the advisories it learnt about and the cursor to resume from. No key, no
account; every source is independent, so one failing is logged and the others run.

OSV is asked two ways: ``querybatch`` by the inventory's purls on every run, so a fresh
install is current in minutes and new software is covered as soon as the map has it, and
the per-ecosystem ``modified_id.csv`` delta, so an advisory already stored is refreshed
when it changes. cvelistV5 is read through ``deltaLog.json``; a CVE is kept when it names
a product the inventory runs (php, node, nginx, ...), is in KEV, or is already stored.
KEV's catalogue version is its cursor. EPSS is daily, for the CVEs stored. The distro
trackers (Ubuntu, Debian, Alpine, ALAS) answer "which package version in which release is
fixed" for the stored CVEs that touch their packages. Packagist and npm add the PHP and
JS advisories at the source.
"""

from __future__ import annotations

import csv
import html
import io
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from captains.patch.advisories import (
    CVE_RE,
    Advisory,
    Affected,
    Range,
    cvss_score,
    fixed_versions,
    product_image,
    ranges_from_constraint,
    severity_label,
)

logger = logging.getLogger("patch.sources")

OSV_API = "https://api.osv.dev/v1"
OSV_DATA = "https://osv-vulnerabilities.storage.googleapis.com"
CVELIST_DELTA = "https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/deltaLog.json"
KEV_URL = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
EPSS_URL = "https://api.first.org/data/v1/epss"
UBUNTU_URL = "https://ubuntu.com/security/cves/{cve}.json"
DEBIAN_URL = "https://security-tracker.debian.org/tracker/{cve}"
ALPINE_URL = "https://secdb.alpinelinux.org/{release}/{repo}.json"
ALAS_URL = "https://explore.alas.aws.amazon.com/{cve}.html"
PACKAGIST_URL = "https://packagist.org/api/security-advisories/"
NPM_BULK_URL = "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk"

UBUNTU_RELEASES = {"bionic": "18.04", "focal": "20.04", "jammy": "22.04", "noble": "24.04", "oracular": "24.10", "plucky": "25.04", "questing": "25.10"}
MAX_PER_RUN = 400            # advisories one source fetches one by one in a run
MAX_CVE_RECORDS = 1500       # cvelistV5 records read in a run; the cursor stays on the last delta read whole, so the rest waits, never lost
FIRST_RUN_DAYS = 3           # how far back a first run reads the CVE deltas
TRACKER_PER_RUN = 100        # CVEs a distro tracker is asked about in a run; the pages are slow
WORKERS = 16                 # parallel fetches of independent records
EPSS_BATCH = 100
QUERYBATCH = 100


@dataclass
class Context:
    http: Any                                  # get(url, params=None) -> text, get_json, post_json
    now: datetime
    inventory: Any                             # captains.patch.inventory.Inventory
    stored_ids: Set[str]                       # every id the store knows (canonical and alias)
    kev_ids: Set[str] = field(default_factory=set)
    cursors: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    lookup: Callable[[str], Optional[Advisory]] = lambda _id: None   # the store's get, for the trackers

    def cursor(self, source: str) -> Any:
        row = self.cursors.get(source)
        return row.get("value") if row else None

    def iso(self) -> str:
        return self.now.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class Result:
    advisories: List[Advisory] = field(default_factory=list)
    cursor: Any = None
    fetched: int = 0
    note: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)


Source = Callable[[Context], Result]


def _iso(value: Any) -> Optional[str]:
    if not value:
        return None
    text = str(value).strip()
    if text.isdigit():
        return datetime.fromtimestamp(int(text), tz=timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return text


FAILED = object()   # a tolerant fetch that raised: unlike None (a 404), it is asked again next run


def _fetch_all(items: Sequence[Any], fetch: Callable[[Any], Any], *, tolerant: bool = False) -> List[Any]:
    """``fetch`` over ``items`` on a few threads, results in order. A failure is re-raised, or with
    ``tolerant`` logged and returned as ``FAILED`` (a tracker's page that times out is tried again)."""
    def one(item: Any) -> Any:
        if not tolerant:
            return fetch(item)
        try:
            return fetch(item)
        except Exception as exc:  # noqa: BLE001
            logger.warning("page for %s not read: %s", getattr(item, "id", item), exc)
            return FAILED
    if len(items) <= 1:
        return [one(i) for i in items]
    with ThreadPoolExecutor(max_workers=min(WORKERS, len(items))) as pool:
        return list(pool.map(one, items))


# ── OSV ───────────────────────────────────────────────────────────────────────

def parse_osv(data: Mapping[str, Any]) -> Advisory:
    """An OSV record as an advisory: every ``affected`` entry with its ecosystem, the ranges'
    events as ranges, the CVSS vector scored."""
    affected: List[Affected] = []
    for entry in data.get("affected") or []:
        package = entry.get("package") or {}
        ecosystem = str(package.get("ecosystem") or "")
        name = str(package.get("name") or "")
        if not name:
            continue
        purl_type, namespace, release = _osv_ecosystem(ecosystem, str(package.get("purl") or ""))
        ranges: List[Range] = []
        for rng in entry.get("ranges") or []:
            current: Optional[Range] = None
            for event in rng.get("events") or []:
                if "introduced" in event:
                    if current is not None:
                        ranges.append(current)
                    current = Range(introduced=str(event["introduced"]), type=str(rng.get("type") or "ECOSYSTEM"))
                elif "fixed" in event and current is not None:
                    current.fixed = str(event["fixed"])
                    ranges.append(current)
                    current = None
                elif "last_affected" in event and current is not None:
                    current.last_affected = str(event["last_affected"])
                    ranges.append(current)
                    current = None
            if current is not None:
                ranges.append(current)
        affected.append(Affected(purl_type=purl_type, name=name, source="osv", namespace=namespace, ecosystem=ecosystem, release=release, ranges=ranges,
                                 versions=[str(v) for v in entry.get("versions") or []], fixed=fixed_versions(ranges),
                                 url=next((r.get("url") for r in data.get("references") or [] if r.get("type") == "ADVISORY"), None)))
    severities = data.get("severity") or []
    vector = next((s.get("score") for s in severities if str(s.get("type", "")).startswith("CVSS_V3")), None)
    score = cvss_score(vector)
    if vector is None:
        vector = next((s.get("score") for s in severities if str(s.get("type", "")).startswith("CVSS_V4")), None)   # v4 only: the label says how bad
    label = str((data.get("database_specific") or {}).get("severity") or "").lower() or None
    ids = [str(data.get("id") or ""), *[str(a) for a in data.get("aliases") or []]]
    return Advisory(id=ids[0], aliases=ids[1:], summary=str(data.get("summary") or "")[:500], details=str(data.get("details") or "")[:4000],
                    cvss=score, cvss_vector=vector, severity=severity_label(score) or label, affected=affected,
                    references=[str(r.get("url")) for r in data.get("references") or [] if r.get("url")][:30],
                    published=data.get("published"), modified=data.get("modified"), withdrawn=data.get("withdrawn"), sources={"osv": str(data.get("modified") or "")})


def _osv_ecosystem(ecosystem: str, purl: str) -> tuple:
    head, _, release = ecosystem.partition(":")
    mapping = {"Packagist": ("composer", None), "npm": ("npm", None), "PyPI": ("pypi", None), "Go": ("golang", None), "crates.io": ("cargo", None),
               "RubyGems": ("gem", None), "Debian": ("deb", "debian"), "Ubuntu": ("deb", "ubuntu"), "Alpine": ("apk", "alpine"), "Maven": ("maven", None),
               "NuGet": ("nuget", None), "Linux": ("generic", None)}
    purl_type, namespace = mapping.get(head, ("generic", None))
    if purl_type == "generic" and purl.startswith("pkg:"):
        purl_type = purl[4:].split("/", 1)[0]
    return purl_type, namespace, release.lstrip("v") or None


def _osv_csv(text: str) -> List[tuple]:
    """``modified_id.csv`` rows as ``(id, modified)`` whichever column order the file uses."""
    out: List[tuple] = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 2:
            continue
        a, b = row[0].strip(), row[1].strip()
        if "T" in a and "-" in a and not b.startswith(("CVE", "GHSA")) and "T" not in b:
            a, b = b, a
        if a and b:
            out.append((a, b))
    return out


def osv(ctx: Context) -> Result:
    result = Result(extra={"queried_purls": 0, "delta_ids": 0})
    cursor = dict(ctx.cursor("osv") or {})
    wanted: Dict[str, str] = {}   # id -> modified seen in the index
    purls = ctx.inventory.osv_purls()
    for i in range(0, len(purls), QUERYBATCH):
        chunk = purls[i:i + QUERYBATCH]
        response = ctx.http.post_json(f"{OSV_API}/querybatch", {"queries": [{"package": {"purl": p}} for p in chunk]})
        result.extra["queried_purls"] += len(chunk)
        for answer in response.get("results") or []:
            for vuln in answer.get("vulns") or []:
                if vuln.get("id"):
                    wanted[str(vuln["id"])] = str(vuln.get("modified") or "")
    for ecosystem in ctx.inventory.osv_ecosystems():
        since = cursor.get(ecosystem)
        rows = _osv_csv(ctx.http.get(f"{OSV_DATA}/{ecosystem}/modified_id.csv"))
        newest = since or ""
        for vuln_id, modified in rows:
            newest = max(newest, modified)
            if since is None:
                continue      # the first run queries by purl; the delta starts from now
            if modified > since and (vuln_id in ctx.stored_ids or vuln_id in wanted):
                wanted[vuln_id] = modified
                result.extra["delta_ids"] += 1
        cursor[ecosystem] = newest or ctx.iso()
    to_fetch: List[str] = []
    for vuln_id, modified in sorted(wanted.items(), key=lambda kv: kv[1], reverse=True):
        known = ctx.lookup(vuln_id) if vuln_id in ctx.stored_ids else None
        if known is not None and modified and str(known.sources.get("osv") or "")[:19] == modified[:19]:
            continue   # stored and unchanged (the index and the record differ in sub-second precision)
        to_fetch.append(vuln_id)
    if len(to_fetch) > MAX_PER_RUN:
        result.note = f"{len(to_fetch) - MAX_PER_RUN} more to fetch next run"
        to_fetch = to_fetch[:MAX_PER_RUN]
    result.advisories.extend(parse_osv(record) for record in _fetch_all(to_fetch, lambda vuln_id: ctx.http.get_json(f"{OSV_API}/vulns/{vuln_id}")))
    result.fetched = len(to_fetch)
    result.cursor = cursor
    return result


# ── cvelistV5 ─────────────────────────────────────────────────────────────────

def parse_cve_record(data: Mapping[str, Any]) -> Advisory:
    meta = data.get("cveMetadata") or {}
    containers = data.get("containers") or {}
    cna = containers.get("cna") or {}
    adps = containers.get("adp") or []
    cve_id = str(meta.get("cveId") or "")
    affected: List[Affected] = []
    for entry in cna.get("affected") or []:
        image = product_image(entry.get("vendor"), entry.get("product"))
        if not image:
            continue
        ranges: List[Range] = []
        versions: List[str] = []
        for v in entry.get("versions") or []:
            if str(v.get("status") or "affected") != "affected":
                continue
            start = str(v.get("version") or "0").replace(".*", ".0")
            if v.get("lessThan"):
                ranges.append(Range(introduced=start, fixed=str(v["lessThan"]), type=str(v.get("versionType") or "SEMVER").upper()))
            elif v.get("lessThanOrEqual"):
                ranges.append(Range(introduced=start, last_affected=str(v["lessThanOrEqual"]), type=str(v.get("versionType") or "SEMVER").upper()))
            elif start and start not in ("0", "n/a", "*"):
                versions.append(start)
        affected.append(Affected(purl_type="docker", name=image, source="cvelist", ranges=ranges, versions=versions, fixed=fixed_versions(ranges), product=image,
                                 ecosystem=f"{entry.get('vendor')}/{entry.get('product')}"))
    score, vector = None, None
    kev: Optional[Dict[str, Any]] = None
    for container in [cna, *adps]:
        for metric in container.get("metrics") or []:
            for key in ("cvssV4_0", "cvssV3_1", "cvssV3_0"):
                block = metric.get(key)
                if isinstance(block, dict) and block.get("baseScore") is not None and (score is None or float(block["baseScore"]) > score):
                    score, vector = float(block["baseScore"]), block.get("vectorString")
            other = metric.get("other") or {}
            if other.get("type") == "kev":
                content = other.get("content") or {}
                kev = {"added": content.get("dateAdded"), "due": content.get("dueDate"), "ransomware": None}
    description = next((d.get("value") for d in cna.get("descriptions") or [] if str(d.get("lang", "en")).startswith("en")), "") or ""
    title = str(cna.get("title") or "")
    return Advisory(id=cve_id, summary=(title or description)[:500], details=str(description)[:4000], cvss=score, cvss_vector=vector, severity=severity_label(score),
                    affected=affected, kev=kev, references=[str(r.get("url")) for r in cna.get("references") or [] if r.get("url")][:30],
                    published=meta.get("datePublished"), modified=meta.get("dateUpdated"), sources={"cvelist": str(meta.get("dateUpdated") or "")})


def cvelist(ctx: Context) -> Result:
    result = Result()
    # the first run reads the last days of deltas, so a fresh install has this week's disclosures; never the month the log holds
    since = ctx.cursor("cvelist") or (ctx.now - timedelta(days=FIRST_RUN_DAYS)).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    log = ctx.http.get_json(CVELIST_DELTA)
    entries = sorted((e for e in (log if isinstance(log, list) else []) if str(e.get("fetchTime") or "") > since), key=lambda e: str(e.get("fetchTime")))
    changed: Dict[str, str] = {}
    cursor = since
    for entry in entries:
        rows = [(str(r.get("cveId")), str(r.get("githubLink")), True) for r in entry.get("new") or [] if r.get("cveId") and r.get("githubLink")]
        rows += [(str(r.get("cveId")), str(r.get("githubLink")), False) for r in entry.get("updated") or [] if r.get("cveId") and r.get("githubLink")]
        # every new record; an update only to a CVE that matters here already (the rest is NVD's enrichment, hundreds an hour)
        wanted = {cve_id: link for cve_id, link, new in rows if new or cve_id in ctx.stored_ids or cve_id in ctx.kev_ids}
        if changed and len(changed) + len(wanted) > MAX_CVE_RECORDS:
            result.note = f"deltas after {cursor} wait for the next run"
            break
        changed.update(wanted)
        cursor = max(cursor, str(entry.get("fetchTime") or ""))
    products = set(ctx.inventory.products())
    ordered = sorted(changed, key=lambda c: (0 if c in ctx.kev_ids else 1 if c in ctx.stored_ids else 2, c))
    records = _fetch_all(ordered, lambda cve_id: ctx.http.get_json(changed[cve_id]))
    for cve_id, record in zip(ordered, records, strict=True):
        advisory = parse_cve_record(record)
        relevant = cve_id in ctx.stored_ids or cve_id in ctx.kev_ids or any(a.product in products for a in advisory.affected)
        if relevant:
            if cve_id in ctx.kev_ids and advisory.kev is None:
                advisory.kev = {"added": None, "due": None, "ransomware": None}
            result.advisories.append(advisory)
    result.fetched = len(ordered)
    result.cursor = cursor
    return result


# ── KEV ───────────────────────────────────────────────────────────────────────

def kev(ctx: Context) -> Result:
    result = Result()
    catalog = ctx.http.get_json(KEV_URL)
    version = str(catalog.get("catalogVersion") or "")
    rows = catalog.get("vulnerabilities") or []
    ids = sorted({str(r.get("cveID")) for r in rows if r.get("cveID")})
    result.extra["ids"] = ids
    if version and version == ctx.cursor("kev"):
        result.cursor = version
        return result
    products = set(ctx.inventory.products())
    for row in rows:
        cve_id = str(row.get("cveID") or "")
        if not cve_id:
            continue
        image = product_image(row.get("vendorProject"), row.get("product"))
        if cve_id not in ctx.stored_ids and image not in products:
            continue
        advisory = Advisory(id=cve_id, summary=str(row.get("vulnerabilityName") or "")[:500], details=str(row.get("shortDescription") or "")[:2000],
                            kev={"added": row.get("dateAdded"), "due": row.get("dueDate"), "ransomware": str(row.get("knownRansomwareCampaignUse") or "").lower() == "known"},
                            references=[str(row["notes"]).split(" ;")[0]] if row.get("notes") else [], sources={"kev": version})
        if image and cve_id not in ctx.stored_ids:
            advisory.affected.append(Affected(purl_type="docker", name=image, source="kev", product=image, ecosystem=f"{row.get('vendorProject')}/{row.get('product')}"))
        result.advisories.append(advisory)
    result.fetched = len(rows)
    result.cursor = version
    return result


# ── EPSS ──────────────────────────────────────────────────────────────────────

def epss(ctx: Context) -> Result:
    result = Result()
    today = ctx.now.date().isoformat()
    if ctx.cursor("epss") == today:
        result.cursor = today
        return result
    cves = sorted(i for i in ctx.stored_ids if CVE_RE.match(i))[: EPSS_BATCH * 10]
    for i in range(0, len(cves), EPSS_BATCH):
        chunk = cves[i:i + EPSS_BATCH]
        data = ctx.http.get_json(EPSS_URL, params={"cve": ",".join(chunk)})
        for row in data.get("data") or []:
            cve_id = str(row.get("cve") or "")
            if not cve_id:
                continue
            result.advisories.append(Advisory(id=cve_id, epss={"score": float(row.get("epss") or 0), "percentile": float(row.get("percentile") or 0), "date": row.get("date")},
                                              sources={"epss": today}))
        result.fetched += len(chunk)
    result.cursor = today
    return result


# ── the distro trackers ──────────────────────────────────────────────────────

def _tracked_cves(ctx: Context, *, wants: Callable[[Advisory], bool], source: str, max_age_days: int = 1) -> List[Advisory]:
    """The stored CVEs a tracker should be asked about: those with an OS package or a product
    among their affected, not asked today."""
    out: List[Advisory] = []
    today = ctx.now.date().isoformat()
    for cve_id in sorted(i for i in ctx.stored_ids if CVE_RE.match(i)):
        advisory = ctx.lookup(cve_id)
        if advisory is None or advisory.id != cve_id:
            continue
        if str(advisory.sources.get(source) or "")[:10] == today:
            continue
        if wants(advisory):
            out.append(advisory)
        if len(out) >= TRACKER_PER_RUN:
            break
    return out


def _has_os_or_product(advisory: Advisory) -> bool:
    return any(a.purl_type in ("deb", "apk", "rpm", "docker") for a in advisory.affected)


def parse_ubuntu(cve_id: str, data: Mapping[str, Any]) -> List[Affected]:
    out: List[Affected] = []
    for package in data.get("packages") or []:
        name = str(package.get("name") or "")
        for status in package.get("statuses") or []:
            codename = str(status.get("release_codename") or "")
            release = UBUNTU_RELEASES.get(codename, codename)
            word = str(status.get("status") or "")
            if word == "DNE":
                continue   # the package does not exist in that release
            version = str(status.get("description") or "").strip()
            fixed = [version] if word == "released" and version and version[0].isdigit() else []
            out.append(Affected(purl_type="deb", name=name, source="ubuntu", namespace="ubuntu", ecosystem=f"Ubuntu:{release}", release=release,
                                fixed=fixed, status=word, url=f"https://ubuntu.com/security/{cve_id}",
                                advisory_id=next((n.get("id") for n in data.get("notices") or [] if isinstance(n, dict) and n.get("id")), None)))
    return out


def ubuntu(ctx: Context) -> Result:
    result = Result()
    if "ubuntu" not in ctx.inventory.distro_releases() and not ctx.inventory.products():
        result.note = "no Ubuntu packages on the map"
        return result
    tracked = _tracked_cves(ctx, wants=_has_os_or_product, source="ubuntu")
    pages = _fetch_all(tracked, lambda a: ctx.http.get_json(UBUNTU_URL.format(cve=a.id), allow_404=True), tolerant=True)
    for advisory, data in zip(tracked, pages, strict=True):
        result.fetched += 1
        if data is FAILED:
            continue   # the page failed: asked again next run
        result.advisories.append(Advisory(id=advisory.id, affected=parse_ubuntu(advisory.id, data) if data else [], sources={"ubuntu": ctx.iso()}))
    result.cursor = ctx.iso()
    return result


_TR_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S | re.I)
_TD_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def _cells(row: str) -> List[str]:
    return [html.unescape(_TAG_RE.sub("", c)).strip() for c in _TD_RE.findall(row)]


def parse_debian(cve_id: str, page: str) -> List[Affected]:
    """The tracker's two tables: the status per release (Source Package | Release | Version | Status)
    and the fixed versions (Package | Type | Release | Fixed Version | Urgency | Origin | Debian Bugs)."""
    status_rows: Dict[tuple, Dict[str, Any]] = {}
    fixed_rows: Dict[tuple, str] = {}
    for row in _TR_RE.findall(page):
        cells = _cells(row)
        if len(cells) < 4 or cells[0] in ("Source Package", "Package"):
            continue
        name = cells[0].split(" ")[0]
        if not name:
            continue
        if cells[1].lower() in ("source", "binary"):
            release = re.sub(r"\s*\(.*?\)", "", cells[2]).strip().lower() or ("sid" if "unstable" in cells[2].lower() else "")
            version = cells[3].strip()
            if release and version and version[0].isdigit():
                fixed_rows[(name, release)] = version
            continue
        release = cells[1].lower().split(" ")[0].split(",")[0]
        status = cells[3].lower()
        if release:
            status_rows[(name, release)] = {"version": cells[2], "status": "fixed" if status.startswith("fixed") else status}
    out: List[Affected] = []
    for (name, release), info in status_rows.items():
        fixed = fixed_rows.get((name, release))
        if not fixed and info["status"] == "fixed" and info["version"] and info["version"][0].isdigit():
            fixed = info["version"]
        out.append(Affected(purl_type="deb", name=name, source="debian", namespace="debian", ecosystem=f"Debian:{release}", release=release,
                            fixed=[fixed] if fixed else [], status=info["status"], url=DEBIAN_URL.format(cve=cve_id)))
    for (name, release), version in fixed_rows.items():
        if (name, release) not in status_rows and release not in ("sid", "unstable"):
            out.append(Affected(purl_type="deb", name=name, source="debian", namespace="debian", ecosystem=f"Debian:{release}", release=release,
                                fixed=[version], status="fixed", url=DEBIAN_URL.format(cve=cve_id)))
    return out


def debian(ctx: Context) -> Result:
    result = Result()
    if "debian" not in ctx.inventory.distro_releases() and not ctx.inventory.products():
        result.note = "no Debian packages on the map"
        return result
    tracked = _tracked_cves(ctx, wants=_has_os_or_product, source="debian")
    pages = _fetch_all(tracked, lambda a: ctx.http.get(DEBIAN_URL.format(cve=a.id), allow_404=True), tolerant=True)
    for advisory, page in zip(tracked, pages, strict=True):
        result.fetched += 1
        if page is FAILED:
            continue   # the page failed: asked again next run
        result.advisories.append(Advisory(id=advisory.id, affected=parse_debian(advisory.id, page) if page else [], sources={"debian": ctx.iso()}))
    result.cursor = ctx.iso()
    return result


def parse_alpine(release: str, repo: str, data: Mapping[str, Any]) -> Dict[str, List[Affected]]:
    """``secdb`` for one release and repository: CVE → the packages and versions that fix it."""
    out: Dict[str, List[Affected]] = {}
    for package in data.get("packages") or []:
        pkg = package.get("pkg") or {}
        name = str(pkg.get("name") or "")
        for version, cves in (pkg.get("secfixes") or {}).items():
            for cve_id in cves or []:
                cve_id = str(cve_id).split(" ")[0].upper()
                if not CVE_RE.match(cve_id):
                    continue
                out.setdefault(cve_id, []).append(Affected(purl_type="apk", name=name, source="alpine", namespace="alpine", ecosystem=f"Alpine:{release}",
                                                           release=release.lstrip("v"), fixed=[str(version)], status="fixed",
                                                           url=f"https://security.alpinelinux.org/vuln/{cve_id}"))
    return out


def alpine(ctx: Context) -> Result:
    result = Result()
    releases = sorted(ctx.inventory.distro_releases().get("alpine") or [])
    if "alpine" not in ctx.inventory.distro_releases():
        result.note = "no Alpine packages on the map"
        return result
    today = ctx.now.date().isoformat()
    if ctx.cursor("alpine") == today:
        result.cursor = today
        return result
    releases = releases or ["3.20", "3.21"]
    by_cve: Dict[str, List[Affected]] = {}
    for release in releases:
        for repo in ("main", "community"):
            data = ctx.http.get_json(ALPINE_URL.format(release=f"v{release}", repo=repo), allow_404=True)
            result.fetched += 1
            for cve_id, entries in parse_alpine(f"v{release}", repo, data or {}).items():
                by_cve.setdefault(cve_id, []).extend(entries)
    for cve_id, entries in by_cve.items():
        if cve_id in ctx.stored_ids:
            result.advisories.append(Advisory(id=cve_id, affected=entries, sources={"alpine": today}))
    result.extra["cves_in_secdb"] = len(by_cve)
    result.cursor = today
    return result


_RPM_VERSION_RE = re.compile(r"^\d[\w.+~]*-\d[\w.+~]*\.(amzn|al)\d", re.I)
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def parse_alas(cve_id: str, page: str) -> List[Affected]:
    """The CVE page's table: one row per ALAS advisory with its package (and, when the page has
    it, the fixed version); the release comes from the advisory id (ALAS2023-…, ALAS2…)."""
    out: List[Affected] = []
    seen: Set[tuple] = set()
    for row in _TR_RE.findall(page):
        cells = [c for c in _cells(row) if c]
        alas_id = next((c for c in cells if c.upper().startswith("ALAS")), None)
        if not alas_id:
            continue
        version = next((c for c in cells if _RPM_VERSION_RE.match(c)), None)
        upper = alas_id.upper()
        release = "2023" if upper.startswith("ALAS2023") else "2"
        package = next((c for c in cells if c not in (alas_id, version) and not _DATE_RE.match(c)
                        and not c.lower().startswith(("al2", "amazon", "critical", "important", "medium", "low", "severity"))), None)
        if not package or (alas_id, package) in seen:
            continue
        seen.add((alas_id, package))
        out.append(Affected(purl_type="rpm", name=package.split(" ")[0], source="alas", namespace="amazonlinux", ecosystem=f"AmazonLinux:{release}", release=release,
                            fixed=[version] if version else [], status="fixed", advisory_id=alas_id, url=ALAS_URL.format(cve=cve_id)))
    return out


def alas(ctx: Context) -> Result:
    result = Result()
    if "amazonlinux" not in ctx.inventory.distro_releases() and not ctx.inventory.products():
        result.note = "no Amazon Linux packages on the map"
        return result
    tracked = _tracked_cves(ctx, wants=_has_os_or_product, source="alas")
    pages = _fetch_all(tracked, lambda a: ctx.http.get(ALAS_URL.format(cve=a.id), allow_404=True), tolerant=True)
    for advisory, page in zip(tracked, pages, strict=True):
        result.fetched += 1
        if page is FAILED:
            continue   # the page failed: asked again next run
        result.advisories.append(Advisory(id=advisory.id, affected=parse_alas(advisory.id, page) if page else [], sources={"alas": ctx.iso()}))
    result.cursor = ctx.iso()
    return result


# ── Packagist and npm ─────────────────────────────────────────────────────────

def parse_packagist(data: Mapping[str, Any]) -> List[Advisory]:
    out: List[Advisory] = []
    for package, rows in (data.get("advisories") or {}).items():
        for row in rows or []:
            ids = [str(row.get("cve") or "").strip() or str(row.get("remoteId") or "").strip() or str(row.get("advisoryId") or "")]
            ids += [str(row.get("advisoryId") or "")] + [str(s.get("remoteId")) for s in row.get("sources") or [] if s.get("remoteId")]
            ids = [i for i in dict.fromkeys(ids) if i]
            ranges = ranges_from_constraint(str(row.get("affectedVersions") or ""))
            name = str(row.get("packageName") or package).lower()
            out.append(Advisory(id=ids[0], aliases=ids[1:], summary=str(row.get("title") or "")[:500],
                                affected=[Affected(purl_type="composer", name=name, source="packagist", ecosystem="Packagist", ranges=ranges, fixed=fixed_versions(ranges),
                                                   advisory_id=row.get("advisoryId"), url=row.get("link"))],
                                references=[str(row["link"])] if row.get("link") else [], published=row.get("reportedAt"), modified=row.get("reportedAt"),
                                sources={"packagist": str(row.get("reportedAt") or "")}))
    return out


def packagist(ctx: Context) -> Result:
    result = Result()
    if not any(i.type == "composer" for i in ctx.inventory.items):
        result.note = "no composer packages on the map"
        return result
    since = ctx.cursor("packagist")
    params = {"updatedSince": str(int(datetime.fromisoformat(str(since).replace("Z", "+00:00")).timestamp()))} if since else {"updatedSince": str(int(ctx.now.timestamp()) - 86400 * 30)}
    data = ctx.http.get_json(PACKAGIST_URL, params=params)
    advisories = parse_packagist(data)
    names = {i.name.lower() for i in ctx.inventory.items if i.type == "composer"}
    result.advisories = [a for a in advisories if any(x.name in names for x in a.affected) or any(i in ctx.stored_ids for i in a.all_ids)]
    result.fetched = len(advisories)
    result.cursor = ctx.iso()
    return result


def parse_npm_bulk(data: Mapping[str, Any]) -> List[Advisory]:
    out: List[Advisory] = []
    for name, rows in (data or {}).items():
        for row in rows or []:
            cves = [str(c) for c in row.get("cve") or [] if c]
            github = str(row.get("url") or "")
            ghsa = github.rsplit("/", 1)[-1] if "GHSA-" in github else None
            ids = [i for i in dict.fromkeys([*cves, *( [ghsa] if ghsa else []), f"NPM-{row.get('id')}" if row.get("id") else ""]) if i]
            if not ids:
                continue
            ranges = ranges_from_constraint(str(row.get("vulnerable_versions") or ""))
            out.append(Advisory(id=ids[0], aliases=ids[1:], summary=str(row.get("title") or "")[:500], severity=str(row.get("severity") or "").lower() or None,
                                affected=[Affected(purl_type="npm", name=str(name), source="npm", ecosystem="npm", ranges=ranges, fixed=fixed_versions(ranges), url=github or None)],
                                references=[github] if github else [], sources={"npm": "bulk"}))
    return out


def npm(ctx: Context) -> Result:
    result = Result()
    packages = ctx.inventory.npm_packages()
    if not packages:
        result.note = "no npm packages on the map"
        return result
    today = ctx.now.date().isoformat()
    if ctx.cursor("npm") == today:
        result.cursor = today
        return result
    names = sorted(packages)
    for i in range(0, len(names), 500):
        body = {name: sorted(packages[name]) for name in names[i:i + 500]}
        data = ctx.http.post_json(NPM_BULK_URL, body)
        result.fetched += len(body)
        for advisory in parse_npm_bulk(data):
            advisory.sources = {"npm": today}
            result.advisories.append(advisory)
    result.cursor = today
    return result


SOURCES: Sequence[tuple] = (
    ("kev", kev), ("osv", osv), ("cvelist", cvelist), ("packagist", packagist), ("npm", npm),
    ("ubuntu", ubuntu), ("debian", debian), ("alpine", alpine), ("alas", alas), ("epss", epss),
)


def names() -> List[str]:
    return [n for n, _ in SOURCES]


def by_name(wanted: Optional[Iterable[str]] = None) -> List[tuple]:
    chosen = set(wanted or names())
    return [(n, fn) for n, fn in SOURCES if n in chosen]
