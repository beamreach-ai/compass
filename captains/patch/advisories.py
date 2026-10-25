"""One vulnerability as the feeds describe it (docs/specs/patching-captain.md §3.1).

An ``Advisory`` is keyed by its CVE id when it has one, else by the id the feed gave it
(a GHSA, a Packagist id). Each feed contributes ``Affected`` entries: a package or a
product, the ecosystem or distro release it lives in, the ranges and versions that are
affected and the versions that fix it, and which feed said so. Two feeds describing the
same CVE merge into one row: aliases unioned, affected entries kept per source, the
highest CVSS kept, KEV and EPSS set by the catalogues that own them.

Pure data and the parsers that are shared by several feeds (CVSS vectors to scores,
version constraints to ranges); the feeds themselves are in ``sources.py``.
"""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

CVE_RE = re.compile(r"^CVE-\d{4}-\d{4,}$", re.IGNORECASE)

# cvelistV5 vendor/product pairs → the docker-library image whose tag is the upstream version
# (docs/specs/patching-captain.md §3.3: "the fixed tag is the one whose upstream version is fixed")
PRODUCT_IMAGES: Dict[str, Sequence[str]] = {
    "php": ("php",),
    "node": ("node.js", "nodejs", "node"),
    "python": ("cpython", "python"),
    "nginx": ("nginx",),
    "httpd": ("http server", "httpd"),
    "ruby": ("ruby",),
    "golang": ("go",),
    "redis": ("redis",),
    "postgres": ("postgresql",),
    "mysql": ("mysql server", "mysql"),
    "mariadb": ("mariadb server", "mariadb"),
    "haproxy": ("haproxy",),
    "traefik": ("traefik",),
    "caddy": ("caddy",),
    "wordpress": ("wordpress",),
    "drupal": ("drupal core", "drupal"),
    "openssl": ("openssl",),
}


@dataclass
class Range:
    introduced: Optional[str] = None
    fixed: Optional[str] = None
    last_affected: Optional[str] = None
    type: str = "ECOSYSTEM"           # SEMVER | ECOSYSTEM | GIT | DEB | CONSTRAINT

    def to_dict(self) -> Dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Affected:
    purl_type: str                    # composer | npm | pypi | golang | cargo | deb | apk | rpm | docker | generic
    name: str                         # "laravel/framework", "php8.3", "php" (a product)
    source: str                       # the feed that said so
    namespace: Optional[str] = None   # the distro of an OS package: ubuntu | debian | alpine | amazonlinux
    ecosystem: Optional[str] = None   # the feed's own ecosystem string: "Packagist", "Debian:12", "Ubuntu:24.04"
    release: Optional[str] = None     # the distro release: "24.04", "bookworm", "3.20", "2023"
    ranges: List[Range] = field(default_factory=list)
    versions: List[str] = field(default_factory=list)   # explicitly listed affected versions
    fixed: List[str] = field(default_factory=list)      # the versions that fix it, from the ranges and the trackers
    status: Optional[str] = None      # a distro tracker's word: released | fixed | needed | not-affected | ignored | DNE
    product: Optional[str] = None     # the docker-library image when the entry is a product (php, node, ...)
    advisory_id: Optional[str] = None   # the feed's own id for this entry (USN-8948-1, ALAS2023-2026-1234, PKSA-...)
    url: Optional[str] = None

    def key(self) -> tuple:
        return (self.source, self.purl_type, self.namespace or "", self.name.lower(), self.ecosystem or "", self.release or "")

    def to_dict(self) -> Dict[str, Any]:
        out = {k: v for k, v in asdict(self).items() if v not in (None, [], "")}
        out["ranges"] = [r.to_dict() for r in self.ranges]
        if not out["ranges"]:
            del out["ranges"]
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Affected":
        return cls(purl_type=str(data.get("purl_type") or ""), name=str(data.get("name") or ""), source=str(data.get("source") or ""),
                   namespace=data.get("namespace"), ecosystem=data.get("ecosystem"), release=data.get("release"),
                   ranges=[Range(**r) for r in data.get("ranges") or []], versions=list(data.get("versions") or []), fixed=list(data.get("fixed") or []),
                   status=data.get("status"), product=data.get("product"), advisory_id=data.get("advisory_id"), url=data.get("url"))


@dataclass
class Advisory:
    id: str
    aliases: List[str] = field(default_factory=list)
    summary: str = ""
    details: str = ""
    cvss: Optional[float] = None
    cvss_vector: Optional[str] = None
    severity: Optional[str] = None    # critical | high | medium | low, from the score or the feed's label
    affected: List[Affected] = field(default_factory=list)
    kev: Optional[Dict[str, Any]] = None      # {"added": date, "due": date, "ransomware": bool}
    epss: Optional[Dict[str, Any]] = None     # {"score": float, "percentile": float, "date": date}
    references: List[str] = field(default_factory=list)
    published: Optional[str] = None
    modified: Optional[str] = None
    withdrawn: Optional[str] = None
    sources: Dict[str, str] = field(default_factory=dict)   # feed -> when it last described this advisory

    @property
    def cve_id(self) -> Optional[str]:
        return next((i for i in [self.id, *self.aliases] if CVE_RE.match(i)), None)

    @property
    def all_ids(self) -> List[str]:
        return [self.id, *[a for a in self.aliases if a != self.id]]

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"id": self.id, "aliases": list(self.aliases), "summary": self.summary, "details": self.details, "cvss": self.cvss,
                               "cvss_vector": self.cvss_vector, "severity": self.severity, "affected": [a.to_dict() for a in self.affected],
                               "kev": self.kev, "epss": self.epss, "references": list(self.references), "published": self.published,
                               "modified": self.modified, "withdrawn": self.withdrawn, "sources": dict(self.sources)}
        return {k: v for k, v in out.items() if v not in (None, [], {}, "")} | {"id": self.id}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Advisory":
        return cls(id=str(data["id"]), aliases=list(data.get("aliases") or []), summary=str(data.get("summary") or ""), details=str(data.get("details") or ""),
                   cvss=float(data["cvss"]) if data.get("cvss") is not None else None, cvss_vector=data.get("cvss_vector"), severity=data.get("severity"),
                   affected=[Affected.from_dict(a) for a in data.get("affected") or []], kev=dict(data["kev"]) if data.get("kev") else None,
                   epss=dict(data["epss"]) if data.get("epss") else None, references=list(data.get("references") or []), published=data.get("published"),
                   modified=data.get("modified"), withdrawn=data.get("withdrawn"), sources=dict(data.get("sources") or {}))


def canonical_id(ids: Iterable[str]) -> str:
    """The CVE id among ``ids`` when there is one, else the first id."""
    listed = [str(i).strip() for i in ids if str(i or "").strip()]
    for candidate in listed:
        if CVE_RE.match(candidate):
            return candidate.upper()
    return listed[0] if listed else ""


def merge(existing: Optional[Advisory], incoming: Advisory) -> Advisory:
    """``existing`` updated with what ``incoming`` says: a feed replaces its own affected
    entries and leaves the other feeds'; the best-known text, the highest CVSS, KEV and EPSS
    from whoever has them; the latest modified."""
    if existing is None:
        out = Advisory.from_dict(incoming.to_dict())
        ids = set(out.all_ids)
        out.id = canonical_id(ids)
        out.aliases = sorted(i for i in ids if i != out.id)
        out.severity = out.severity or severity_label(out.cvss)
        return out
    out = Advisory.from_dict(existing.to_dict())
    ids = set(out.all_ids) | set(incoming.all_ids)
    out.id = canonical_id(ids)
    out.aliases = sorted(i for i in ids if i != out.id)
    incoming_sources = set(incoming.sources) or {a.source for a in incoming.affected}
    kept = [a for a in out.affected if a.source not in incoming_sources]
    seen = set()
    for entry in incoming.affected:
        if entry.key() not in seen:
            seen.add(entry.key())
            kept.append(entry)
    out.affected = kept
    # a newer description replaces the text; between two of the same age the fuller one stays
    newer = bool(incoming.modified) and incoming.modified >= (out.modified or "")
    if incoming.summary and (newer or len(incoming.summary) > len(out.summary or "")):
        out.summary = incoming.summary
    if incoming.details and (newer or len(incoming.details) > len(out.details or "")):
        out.details = incoming.details
    if incoming.cvss is not None and (out.cvss is None or incoming.cvss > out.cvss):
        out.cvss, out.cvss_vector = incoming.cvss, incoming.cvss_vector or out.cvss_vector
    out.severity = severity_label(out.cvss) or incoming.severity or out.severity
    if incoming.kev is not None:
        # the catalogue knows the ransomware flag, the CVE record's ADP container only the dates: keep what each knows
        out.kev = {**(out.kev or {}), **{k: v for k, v in incoming.kev.items() if v is not None}}
    if incoming.epss is not None:
        out.epss = incoming.epss
    out.references = sorted(set(out.references) | set(incoming.references))
    out.published = min(filter(None, (out.published, incoming.published)), default=None)
    out.modified = max(filter(None, (out.modified, incoming.modified)), default=None)
    out.withdrawn = incoming.withdrawn or out.withdrawn
    out.sources.update(incoming.sources)
    return out


def severity_label(score: Optional[float]) -> Optional[str]:
    if score is None:
        return None
    if score >= 9.0:
        return "critical"
    if score >= 7.0:
        return "high"
    if score >= 4.0:
        return "medium"
    if score > 0:
        return "low"
    return "none"


# ── CVSS ──────────────────────────────────────────────────────────────────────

_CVSS3 = {
    "AV": {"N": 0.85, "A": 0.62, "L": 0.55, "P": 0.2}, "AC": {"L": 0.77, "H": 0.44},
    "PR": {"N": (0.85, 0.85), "L": (0.62, 0.68), "H": (0.27, 0.5)}, "UI": {"N": 0.85, "R": 0.62},
    "CIA": {"H": 0.56, "L": 0.22, "N": 0.0},
}


def _roundup(value: float) -> float:
    whole = round(value * 100000)
    return whole / 100000.0 if whole % 10000 == 0 else (math.floor(whole / 10000) + 1) / 10.0


def cvss_score(vector: Optional[str]) -> Optional[float]:
    """The base score of a CVSS 3.0 or 3.1 vector (the arithmetic of the specification); None
    for anything else, including v4, whose score the feeds give alongside the vector."""
    if not vector or not vector.upper().startswith("CVSS:3"):
        return None
    parts = dict(p.split(":", 1) for p in vector.split("/")[1:] if ":" in p)
    try:
        changed = parts["S"] == "C"
        iss = 1 - (1 - _CVSS3["CIA"][parts["C"]]) * (1 - _CVSS3["CIA"][parts["I"]]) * (1 - _CVSS3["CIA"][parts["A"]])
        impact = 7.52 * (iss - 0.029) - 3.25 * (iss - 0.02) ** 15 if changed else 6.42 * iss
        exploitability = 8.22 * _CVSS3["AV"][parts["AV"]] * _CVSS3["AC"][parts["AC"]] * _CVSS3["PR"][parts["PR"]][1 if changed else 0] * _CVSS3["UI"][parts["UI"]]
    except KeyError:
        return None
    if impact <= 0:
        return 0.0
    return _roundup(min((1.08 if changed else 1.0) * (impact + exploitability), 10))


# ── version constraints ───────────────────────────────────────────────────────

_OP_RE = re.compile(r"^(>=|<=|>|<|==|=|!=|~|\^)?\s*v?([0-9A-Za-z][0-9A-Za-z.+\-]*)$")


def ranges_from_constraint(text: str) -> List[Range]:
    """Composer's and npm's ``>=11.0.0,<11.2.1 || >=12,<12.0.3`` (comma or space is AND, ``|``/``||``
    is OR) as ranges: ``>=`` and ``>`` open one, ``<`` fixes it, ``<=`` is the last affected; a
    bare version is that version alone."""
    out: List[Range] = []
    for clause in re.split(r"\s*\|\|?\s*", (text or "").strip()):
        if not clause:
            continue
        current = Range(type="CONSTRAINT")
        exact: List[str] = []
        for token in re.split(r"[,\s]+", clause.strip()):
            match = _OP_RE.match(token.strip())
            if not match:
                continue
            op, version = match.group(1) or "=", match.group(2)
            if op in (">=", ">"):
                current.introduced = version
            elif op == "<":
                current.fixed = version
            elif op == "<=":
                current.last_affected = version
            elif op in ("=", "=="):
                exact.append(version)
        if exact and not (current.introduced or current.fixed or current.last_affected):
            for version in exact:
                out.append(Range(introduced=version, last_affected=version, type="CONSTRAINT"))
        elif current.introduced or current.fixed or current.last_affected:
            if current.introduced is None:
                current.introduced = "0"
            out.append(current)
    return out


def fixed_versions(ranges: Iterable[Range]) -> List[str]:
    return sorted({r.fixed for r in ranges if r.fixed})


def product_image(vendor: Any, product: Any) -> Optional[str]:
    """The docker-library image for a CVE record's vendor/product, when it is one we follow."""
    text = f"{vendor or ''} {product or ''}".lower()
    product_text = str(product or "").lower().strip()
    for image, names in PRODUCT_IMAGES.items():
        for name in names:
            if product_text == name or (len(name) > 3 and name in text):
                return image
    return None
