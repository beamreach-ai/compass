"""The registry of pull requests Compass opened.

One item per PR under the workspace partition (``PR#{owner}/{repo}#{number}``),
written once by the writer that opened it and updated by the PR watcher. The
``intent`` block is immutable after creation: it is what a revision must keep.
Without an items table (local dev, unit tests) the registry is in-memory.
"""

from __future__ import annotations

import os
import re
import threading
import time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from compass_core.keys import pr_prefix, pr_sort_key, workspace_from_config, workspace_pk
from compass_core.tables import tables

# any host: github.com, a GitHub Enterprise Server (GITHUB_API_URL), the end-to-end suite's fake
_PR_URL_RE = re.compile(r"https?://[^/\s]+/([\w.-]+/[\w.-]+)/pull/(\d+)")

_MEMORY: Dict[str, Dict[str, Dict[str, Any]]] = {}
_MEMORY_LOCK = threading.Lock()


def reset_memory_for_tests() -> None:
    with _MEMORY_LOCK:
        _MEMORY.clear()


def parse_pr_url(url: Optional[str]) -> Optional[Tuple[str, int]]:
    if not isinstance(url, str):
        return None
    match = _PR_URL_RE.search(url.strip())
    if not match:
        return None
    return match.group(1), int(match.group(2))


def _now_ms() -> int:
    return int(time.time() * 1000)


def _to_native(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, dict):
        return {k: _to_native(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_to_native(v) for v in value]
    return value


def _to_dynamo(value: Any) -> Any:
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items() if v is not None or k in ("reason",)}
    if isinstance(value, list):
        return [_to_dynamo(v) for v in value]
    if isinstance(value, str) and value == "":
        return value
    return value


def new_record(
    *,
    url: str,
    pr_type: str,
    origin: Dict[str, Any],
    intent: Dict[str, Any],
    branch: Optional[str],
    base_branch: Optional[str],
    title: Optional[str] = None,
) -> Dict[str, Any]:
    parsed = parse_pr_url(url)
    if not parsed:
        raise ValueError(f"not a GitHub pull request URL: {url!r}")
    repo, number = parsed
    now = _now_ms()
    return {
        "repo": repo,
        "number": number,
        "url": url.strip(),
        "title": title,
        "branch": branch,
        "base_branch": base_branch or "main",
        "type": pr_type,
        "origin": dict(origin or {}),
        "intent": dict(intent or {}),
        "state": "open",
        "github": {},
        "cursor": {},
        "watch": {"status": "watching", "reason": None, "next_poll_at": None, "backoff_level": 0},
        "revisions": [],
        "decisions": [],
        "pending_revision": None,
        "merge_policy": None,
        "counters": {"revisions": 0, "refusals": 0, "human_commits": 0},
        "created_at": now,
        "updated_at": now,
    }


class PRRegistry:
    def __init__(self, table_name: Optional[str], workspace_id: str, region: Optional[str] = None):
        self.table_name = (table_name or "").strip() or None
        self.workspace_id = workspace_id
        self._region = region or os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION")
        self._table = None

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]], workspace_id: Optional[str] = None) -> "PRRegistry":
        config = config if isinstance(config, dict) else {}
        dynamodb_config = config.get("dynamodb_config") if isinstance(config.get("dynamodb_config"), dict) else {}
        table = tables(dynamodb_config).items
        ws = workspace_from_config(dynamodb_config, explicit=workspace_id or config.get("workspace_id"))
        return cls(table, ws)

    @classmethod
    def from_dynamodb_config(cls, dynamodb_config: Optional[Dict[str, Any]], workspace_id: Optional[str] = None) -> "PRRegistry":
        dynamodb_config = dynamodb_config if isinstance(dynamodb_config, dict) else {}
        table = tables(dynamodb_config).items
        return cls(table, workspace_from_config(dynamodb_config, explicit=workspace_id))

    # ── storage ────────────────────────────────────────────────────────────

    def _pk(self) -> str:
        return workspace_pk(self.workspace_id)

    def _dynamo_table(self):
        if self._table is None:
            import boto3

            self._table = boto3.resource("dynamodb", region_name=self._region).Table(self.table_name)
        return self._table

    def _mem(self) -> Dict[str, Dict[str, Any]]:
        return _MEMORY.setdefault(self._pk(), {})

    def _put(self, record: Dict[str, Any]) -> Dict[str, Any]:
        record["updated_at"] = _now_ms()
        sk = pr_sort_key(record["repo"], record["number"])
        if not self.table_name:
            with _MEMORY_LOCK:
                self._mem()[sk] = _to_native(record)
            return record
        item = _to_dynamo({**record, "pk": self._pk(), "sk": sk})
        self._dynamo_table().put_item(Item=item)
        return record

    def get(self, repo: str, number: int) -> Optional[Dict[str, Any]]:
        sk = pr_sort_key(repo, number)
        if not self.table_name:
            with _MEMORY_LOCK:
                item = self._mem().get(sk)
            return dict(item) if item else None
        resp = self._dynamo_table().get_item(Key={"pk": self._pk(), "sk": sk})
        item = resp.get("Item")
        if not item:
            return None
        item = _to_native(item)
        item.pop("pk", None)
        item.pop("sk", None)
        return item

    def get_by_url(self, url: str) -> Optional[Dict[str, Any]]:
        parsed = parse_pr_url(url)
        return self.get(*parsed) if parsed else None

    def list(self, *, state: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.table_name:
            with _MEMORY_LOCK:
                items = [dict(v) for v in self._mem().values()]
        else:
            from boto3.dynamodb.conditions import Key

            items = []
            kwargs: Dict[str, Any] = {
                "KeyConditionExpression": Key("pk").eq(self._pk()) & Key("sk").begins_with(pr_prefix()),
            }
            while True:
                resp = self._dynamo_table().query(**kwargs)
                for raw in resp.get("Items", []):
                    raw = _to_native(raw)
                    raw.pop("pk", None)
                    raw.pop("sk", None)
                    items.append(raw)
                last = resp.get("LastEvaluatedKey")
                if not last or len(items) >= limit * 3:
                    break
                kwargs["ExclusiveStartKey"] = last
        if state:
            items = [i for i in items if str(i.get("state") or "") == state]
        items.sort(key=lambda i: i.get("updated_at") or i.get("created_at") or 0, reverse=True)
        return items[:limit]

    # ── writes ─────────────────────────────────────────────────────────────

    def record(
        self,
        *,
        url: str,
        pr_type: str,
        origin: Dict[str, Any],
        intent: Dict[str, Any],
        branch: Optional[str],
        base_branch: Optional[str],
        title: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create the record, or refresh the mutable parts of an existing one.

        The intent is written once: a second plant/import on the same branch
        keeps the original purpose."""
        fresh = new_record(url=url, pr_type=pr_type, origin=origin, intent=intent, branch=branch, base_branch=base_branch, title=title)
        existing = self.get(fresh["repo"], fresh["number"])
        if existing:
            existing["branch"] = branch or existing.get("branch")
            existing["base_branch"] = base_branch or existing.get("base_branch")
            existing["title"] = title or existing.get("title")
            existing_origin = dict(existing.get("origin") or {})
            for key, value in (origin or {}).items():
                existing_origin.setdefault(key, value)
            existing["origin"] = existing_origin
            if existing.get("state") in ("closed", "merged"):
                existing["state"] = "open"
                existing["watch"] = {"status": "watching", "reason": None, "next_poll_at": None, "backoff_level": 0}
            return self._put(existing)
        return self._put(fresh)

    def update(self, repo: str, number: int, **fields: Any) -> Optional[Dict[str, Any]]:
        record = self.get(repo, number)
        if not record:
            return None
        fields.pop("intent", None)
        record.update(fields)
        return self._put(record)

    def append_revision(self, repo: str, number: int, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        record = self.get(repo, number)
        if not record:
            return None
        revisions = list(record.get("revisions") or [])
        revisions.append({"at": _now_ms(), **entry})
        counters = dict(record.get("counters") or {})
        if entry.get("outcome") == "pushed":
            counters["revisions"] = int(counters.get("revisions") or 0) + 1
        elif entry.get("outcome") == "refused":
            counters["refusals"] = int(counters.get("refusals") or 0) + 1
        record["revisions"] = revisions
        record["counters"] = counters
        return self._put(record)

    def append_decision(self, repo: str, number: int, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        record = self.get(repo, number)
        if not record:
            return None
        decisions = list(record.get("decisions") or [])
        decisions.append({"at": _now_ms(), **entry})
        record["decisions"] = decisions[-50:]
        return self._put(record)

    def set_watch(self, repo: str, number: int, status: str, reason: Optional[str] = None) -> Optional[Dict[str, Any]]:
        record = self.get(repo, number)
        if not record:
            return None
        watch = dict(record.get("watch") or {})
        watch["status"] = status
        watch["reason"] = reason
        record["watch"] = watch
        return self._put(record)
