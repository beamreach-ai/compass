"""Version order per ecosystem, so "is 8.3.30 before 8.3.36" and "is
8.3.6-0ubuntu0.24.04.12 before 8.3.6-0ubuntu0.24.04.13" are answered the way the
package manager answers them: Debian's and Ubuntu's ``dpkg`` ordering for deb (Alpine's
``-rN`` fits it), ``rpmvercmp`` for rpm, PEP 440 for PyPI, and a semver-like order for
composer, npm, cargo, Go modules and docker-library tags, with a pre-release before its
release. ``affected`` applies an advisory's ranges to one version."""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Tuple

from packaging.version import InvalidVersion, Version

from captains.patch.advisories import Range

DPKG_TYPES = frozenset({"deb", "apk"})
RPM_TYPES = frozenset({"rpm"})


# ── dpkg ──────────────────────────────────────────────────────────────────────

def _order(char: str) -> int:
    if char == "~":
        return -1
    if char.isdigit():
        return 0
    if char.isalpha():
        return ord(char)
    return ord(char) + 256 if char else 0


def _verrevcmp(a: str, b: str) -> int:
    i = j = 0
    while i < len(a) or j < len(b):
        first_diff = 0
        while (i < len(a) and not a[i].isdigit()) or (j < len(b) and not b[j].isdigit()):
            ac = _order(a[i]) if i < len(a) else 0
            bc = _order(b[j]) if j < len(b) else 0
            if ac != bc:
                return -1 if ac < bc else 1
            i += 1
            j += 1
        while i < len(a) and a[i] == "0":
            i += 1
        while j < len(b) and b[j] == "0":
            j += 1
        while i < len(a) and a[i].isdigit() and j < len(b) and b[j].isdigit():
            if first_diff == 0:
                first_diff = (a[i] > b[j]) - (a[i] < b[j])
            i += 1
            j += 1
        if i < len(a) and a[i].isdigit():
            return 1
        if j < len(b) and b[j].isdigit():
            return -1
        if first_diff:
            return first_diff
    return 0


def _dpkg_parts(version: str) -> Tuple[int, str, str]:
    epoch, _, rest = version.partition(":") if ":" in version else ("0", "", version)
    upstream, _, revision = rest.rpartition("-") if "-" in rest else (rest, "", "")
    try:
        return int(epoch), upstream, revision
    except ValueError:
        return 0, version, ""


def dpkg_compare(a: str, b: str) -> int:
    ea, ua, ra = _dpkg_parts(a.strip())
    eb, ub, rb = _dpkg_parts(b.strip())
    if ea != eb:
        return -1 if ea < eb else 1
    result = _verrevcmp(ua, ub)
    return result if result else _verrevcmp(ra, rb)


# ── rpm ───────────────────────────────────────────────────────────────────────

_RPM_SEGMENT = re.compile(r"(\d+|[a-zA-Z]+|~|\^)")


def rpm_compare(a: str, b: str) -> int:
    """``rpmvercmp`` over ``[epoch:]version[-release]``: numeric segments before alphabetic, ``~`` sorts
    before anything, longer digit runs win, a trailing release compares after the version."""
    def parts(v: str) -> Tuple[int, str]:
        epoch, _, rest = v.partition(":") if ":" in v else ("0", "", v)
        try:
            return int(epoch), rest
        except ValueError:
            return 0, v

    def cmp(x: str, y: str) -> int:
        xs, ys = _RPM_SEGMENT.findall(x), _RPM_SEGMENT.findall(y)
        for sx, sy in zip(xs, ys, strict=False):
            if sx == sy:
                continue
            if sx == "~" or sy == "~":
                return -1 if sx == "~" else 1
            if sx.isdigit() and sy.isdigit():
                nx, ny = int(sx), int(sy)
                if nx != ny:
                    return -1 if nx < ny else 1
                continue
            if sx.isdigit() != sy.isdigit():
                return 1 if sx.isdigit() else -1
            if sx != sy:
                return -1 if sx < sy else 1
        if len(xs) == len(ys):
            return 0
        longer = xs if len(xs) > len(ys) else ys
        if longer[len(min(xs, ys, key=len))] == "~":
            return 1 if longer is ys else -1
        return 1 if longer is xs else -1

    ea, ra = parts(a.strip())
    eb, rb = parts(b.strip())
    if ea != eb:
        return -1 if ea < eb else 1
    va, _, rela = ra.partition("-")
    vb, _, relb = rb.partition("-")
    result = cmp(va, vb)
    return result if result else cmp(rela, relb)


# ── semver-like and PEP 440 ───────────────────────────────────────────────────

_TOKEN = re.compile(r"\d+|[A-Za-z]+")


def _semver_key(version: str) -> Tuple[Tuple[int, ...], Tuple[Tuple[int, object], ...]]:
    text = version.strip().lstrip("vV")
    text = text.split("+", 1)[0]
    main, _, pre = text.partition("-")
    numbers: List[int] = []
    rest = ""
    for i, part in enumerate(main.split(".")):
        if part.isdigit():
            numbers.append(int(part))
        else:
            rest = ".".join(main.split(".")[i:])
            break
    pre = ".".join(p for p in (rest, pre) if p)
    pre_key: Tuple[Tuple[int, object], ...] = ()
    if pre:
        pre_key = tuple((0, int(t)) if t.isdigit() else (1, t.lower()) for t in _TOKEN.findall(pre))
    while len(numbers) < 3:
        numbers.append(0)
    return tuple(numbers), pre_key


def semver_compare(a: str, b: str) -> int:
    na, pa = _semver_key(a)
    nb, pb = _semver_key(b)
    if na != nb:
        return -1 if na < nb else 1
    if pa == pb:
        return 0
    if not pa:
        return 1        # a release is after its pre-releases
    if not pb:
        return -1
    return -1 if pa < pb else 1


def pep440_compare(a: str, b: str) -> int:
    try:
        va, vb = Version(a), Version(b)
    except InvalidVersion:
        return semver_compare(a, b)
    return (va > vb) - (va < vb)


def compare(purl_type: str, a: str, b: str) -> int:
    """-1, 0 or 1 as the ecosystem's package manager orders ``a`` against ``b``."""
    if purl_type in DPKG_TYPES:
        return dpkg_compare(a, b)
    if purl_type in RPM_TYPES:
        return rpm_compare(a, b)
    if purl_type == "pypi":
        return pep440_compare(a, b)
    return semver_compare(a, b)


# ── ranges ────────────────────────────────────────────────────────────────────

def in_range(purl_type: str, version: str, rng: Range) -> bool:
    if rng.introduced and rng.introduced not in ("0",) and compare(purl_type, version, rng.introduced) < 0:
        return False
    if rng.fixed and compare(purl_type, version, rng.fixed) >= 0:
        return False
    if rng.last_affected and compare(purl_type, version, rng.last_affected) > 0:
        return False
    return True


def affected(purl_type: str, version: str, ranges: Iterable[Range], versions: Iterable[str] = ()) -> bool:
    """Whether ``version`` is in any of the ranges or listed explicitly."""
    listed = list(versions)
    ranges = list(ranges)
    if listed and any(compare(purl_type, version, v) == 0 for v in listed):
        return True
    return any(in_range(purl_type, version, r) for r in ranges)


def major(purl_type: str, version: str) -> Optional[str]:
    if purl_type in DPKG_TYPES or purl_type in RPM_TYPES:
        return None
    match = re.match(r"^v?(\d+)", version.strip())
    return match.group(1) if match else None


def choose_fix(purl_type: str, current: Optional[str], fixed: Iterable[str]) -> Tuple[Optional[str], bool]:
    """The lowest fixed version above ``current``, preferring one in the same major; the flag says
    the only fix is a major upgrade. Without a current version, the lowest fix."""
    candidates = sorted({f for f in fixed if f}, key=lambda f: _sort_key(purl_type, f))
    if not candidates:
        return None, False
    if not current:
        return candidates[0], False
    above = [f for f in candidates if compare(purl_type, f, current) > 0]
    if not above:
        return None, False
    same_major = [f for f in above if major(purl_type, f) == major(purl_type, current)]
    if same_major:
        return same_major[0], False
    return above[0], major(purl_type, current) is not None


class _Sortable:
    def __init__(self, purl_type: str, value: str):
        self.purl_type, self.value = purl_type, value

    def __lt__(self, other: "_Sortable") -> bool:
        return compare(self.purl_type, self.value, other.value) < 0

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Sortable) and compare(self.purl_type, self.value, other.value) == 0


def _sort_key(purl_type: str, value: str) -> _Sortable:
    return _Sortable(purl_type, value)


# ── docker-library tags ───────────────────────────────────────────────────────

_TAG_VERSION = re.compile(r"^v?(\d+(?:\.\d+)*)")


def tag_version(tag: Optional[str]) -> Tuple[Optional[str], bool]:
    """``(upstream version, floating)`` of a docker-library tag: ``8.3.30-fpm`` → (``8.3.30``, False);
    ``8.3-fpm`` → (``8.3``, True): a series, whatever was pulled; ``latest`` → (None, True)."""
    if not tag:
        return None, True
    match = _TAG_VERSION.match(tag)
    if not match:
        return None, True
    version = match.group(1)
    return version, version.count(".") < 2


def fixed_tag(tag: str, fixed_version: str) -> str:
    """The same tag at the fixed upstream version: ``8.3.30-fpm-alpine3.20`` → ``8.3.36-fpm-alpine3.20``."""
    current, _ = tag_version(tag)
    return tag.replace(current, fixed_version, 1) if current else fixed_version
