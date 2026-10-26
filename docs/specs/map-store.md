# The map at scale: a graph store, SQLite, a query API, history

Status: accepted 2026-10-19; SQLite is the open default (decision 1). Roadmap items 1.10, 1.1, 1.2, 1.3, 1.4 (angle 1).
Line references are to `main` at 83e34fa.

## 1. Goals and non-goals

**Goals.**

- The map stays fast and complete at 100k resources and beyond: a scan writes
  in minutes, the dashboard opens in seconds, a question about one resource
  answers in milliseconds, and nothing is silently cut off.
- Multi-hop questions (what owns this, what can reach that, what sits next to
  this role) are answered by the store, not by loading the whole graph into
  Python per request.
- History: a scan no longer deletes what it did not see. The map can be read
  as it was, and two scans can be diffed.
- One interface, `GraphStore`, that the mapper writes through and every reader
  reads through, so a larger deployment can plug in its own store the way it
  plugs in its queue and config today.
- The store and the graph schema are cloud-neutral. Azure is the likely second
  provider; AWS specifics live in node properties, not in column names or
  method signatures.
- DynamoDB stays for jobs, state, decoys and everything that is not the graph.

**Non-goals (for now; the design leaves room, nothing more).**

- 1.5 faster and incremental scans: the write path takes a `scope` so a
  partial scan can one day close only its own rows, but the mapper keeps
  scanning everything.
- 1.6 coverage gaps.
- 1.7 effective-access edges and attack paths: typed edges and a path query
  exist in the interface; no `can_read` edge is produced yet.
- 1.8 multi-account: `account` is a column, `org_id` stays the scope key; no
  cross-account joins are designed here.
- 1.9 import and export: `mapper export` keeps working through the interface;
  no new formats.
- A per-captain session broker: the store never sees target credentials
  today (it takes the control session, `infra_map/store.py:48`) and will not
  tomorrow.
- A UI redesign. Section 8 says what the API must support so the map can stop
  loading everything; the look of the map does not change.

## 2. Current state

What the code does today, from reading it (Phase 0).

**Collection.** `AWSGraphCollector.collect` (`captains/mapper/aws_mapper.py:458`)
walks regions one after another, 17 service collectors per region, S3 and IAM
once. GitHub and Terraform-state collectors yield *fact batches*
(`infra_map/facts.py`) that a materializer turns into nodes and edges. The
whole graph is built in memory, then coverage and drift are annotated.

**Store: two write paths in one table.** `AWSInfraMapper.run`
(`aws_mapper.py:198-215`) calls `fetch_graph` for the existing graph, then one
`put_item` per node and edge, then one `delete_item` per stale node and edge.
Separately, every GitHub, Terraform-state and derived batch goes through
`InfraMapFactStore.replace_scope` (`infra_map/fact_store.py:39-67`), which
lists the scope's facts (one unpaginated query) and deletes then rewrites each
one. Facts are written and never read by anything but that listing.

Every map item of a workspace shares the partition key `WORKSPACE#<id>`
(`compass_core/keys.py:16`), with sort keys `MAP#ORG#<org>#NODE#<id>`,
`MAP#ORG#<org>#REL#FROM#<id>#<edge>` and `MAP#FACT#ORG#...`. Two GSIs exist
(`compass_core/commands.py:29-37`): `ResourceTypeIndex` (gsi1) is written and
never queried; `TargetResourceIndex` (gsi2) serves incoming-edge lookups only.
TTL is enabled on `expires_at` (`commands.py:204`), which nothing sets.

**Reads.** `fetch_graph` (`store.py:252`) pages through the partition and
deserialises every item into a dataclass; `limit` counts items, not nodes, and
truncates silently. `list_outgoing_relationships` and
`list_incoming_relationships` (`store.py:118-154`) issue one query each with
no pagination: past 1 MB per query the result is cut, so `neighbors`
(`store.py:156-211`, one query per node per level per direction) is already
wrong for hub nodes such as a role used by thousands of functions.

**API.** `GET /api/maps` (`compass_api/routes/maps.py:62-108`) returns the
whole graph up to `limit` (100 to 10000, default 2000) with no marker that it
was cut, or a neighbourhood when `focus` is set. The store is built per request
(`routes/_infra_map.py:56`) from the table name in `dynamodb_config.map`.

**Dashboard.** One request with `limit: 4000`
(`dashboard/app/components/MapsTab.tsx:1005-1008`); type filter, search and
focus are client-side (`:1100-1116`); Graphin draws everything (`:934`). The
API's `focus` is used only by the CLI (`cli/src/tools/map.ts:44-51`).

**Whole-graph loads per request or per job.** 13 production call sites:
`/api/maps` (`maps.py:93`), IaC coverage (`:158`), IaC suggestions (`:297`),
decoy suggestions (`routes/honeypots.py:265`), repo list (`:354`), ownership
(`captains/mapper/ownership_resolver.py:100`), the librarian
(`captains/librarian/service.py:25`), the honeypot captain twice per
placement (`captains/honeypot/captain.py:533`, `:570`), the investigation
(`captains/honeypot/investigate.py:74`), fix-drift
(`captains/iac/fix_drift_job.py:84`), the export (`captains/mapper/export.py:21`)
and the mapper itself. Point reads: `get_resource` (`captains/prs/context.py:242`,
`investigate.py:47`), `batch_get_resources` (`captains/iac/import_job.py:244`,
`fix_drift_job.py:32`), `neighbors` (`investigate.py:123`). Eight places
construct the store by table name.

**Partial scans** raise "not implemented" (`captains/mapper/commands.py:14`,
`maps.py:139`, HTTP 501). **History**: none; stale rows are deleted.
`InfraMapStore.replace_scope` (`store.py:297`) is dead code.

**Migration hook.** `compass migrate` (`compass_core/migrations/__init__.py`)
runs on `serve` and `api` start under the extensions lock; a step is
`(version, callable(config))`, recorded once per workspace in the items table,
must be safe if the newer daemon already wrote, and must never delete.

**Tests.** The store unit tests use `MagicMock`, not moto. No test covers
`/api/maps`, `/api/iac/coverage` or `/api/iac/suggestions`. The mapper tests
fake the store with five methods (`tests/captains/mapper/test_aws_mapper.py:55-74`):
`put_resource`, `put_relationship`, `fetch_graph`, `delete_resource`,
`delete_relationship`. The e2e suite seeds a seven-resource account in moto
(`e2e/fixture.py`).

**Deployment facts that shape the design.** The API and the daemon are two
containers from one image, sharing bind mounts `./config`, `./map`,
`./extensions` (`docker-compose.yml`). Only the daemon runs jobs, so only the
daemon scans; the CLI runs commands inline, so `compass mapper run` from the
shell is a second writer. The image is `python:3.12-slim` (Debian bookworm,
SQLite 3.46 in the built image: recursive CTEs, JSON functions, `RETURNING`, partial indexes,
FTS5 all present).

## 3. The `GraphStore` interface

A protocol in `infra_map/graph_store.py`. A registry next to it,
`infra_map.graph_store.open_store(config, workspace_id)` with
`set_graph_store_factory(fn)`, follows `compass_core.queue`: the open default
chooses a backend from the workspace config; a host replaces the factory at
import time. The eight construction sites become calls to `open_store`.

### 3.1 Vocabulary

- **Node**: a thing in the map with a provider-canonical id (an ARN on AWS,
  a resource id on Azure, `github:repo:<owner>/<name>`, `tf:root:<repo>/<path>`).
  Columns are the few things every provider has: `type`, `name`, `provider`,
  `account`, `region`, `source`, `scope`. Everything else is `props` (tags,
  attributes, coverage, drift, terraform metadata).
- **Edge**: directed, typed, from one node id to another. Types are verbs
  (`secured_by`, `observes`, `corresponds_to`, and later `can_read`,
  `can_assume`). An edge id is the SHA-1 of `from->to|type`, as today.
- **Scan**: one run of a collector set over a scope, with a start and an end.
  Every row's validity is bounded by scans.
- **Scope**: `(source, scope)` as today (`aws_scan`/`<account>`,
  `github`/`<repo>`, `tfstate`/`<bucket>/<key>`, `derived`/`<repo>`). A scan
  names the scopes it covers; rows outside them are untouched.

`ResourceNode` and `RelationshipEdge` (`infra_map/models.py`) stay the Python
types; `account_id` keeps its name in Python for compatibility and maps to
the `account` column. A `provider` field is added, defaulting to `aws`.

### 3.2 Methods

```python
class GraphStore(Protocol):
    workspace_id: str

    # ---- point reads
    def get_node(self, org_id: str, node_id: str, *, as_of: int | None = None) -> ResourceNode | None: ...
    def get_nodes(self, org_id: str, node_ids: Sequence[str], *, as_of: int | None = None) -> dict[str, ResourceNode]: ...

    # ---- paged reads (keyset pagination; cursor is opaque)
    def query_nodes(self, org_id: str, q: NodeQuery, *, cursor: str | None = None, limit: int = 500) -> Page[ResourceNode]: ...
    def query_edges(self, org_id: str, q: EdgeQuery, *, cursor: str | None = None, limit: int = 1000) -> Page[RelationshipEdge]: ...

    # ---- graph reads
    def edges_of(self, org_id: str, node_id: str, *, direction: Direction = "both",
                 types: Sequence[str] | None = None, limit: int = 5000, as_of: int | None = None) -> list[RelationshipEdge]: ...
    def neighborhood(self, org_id: str, node_id: str, *, depth: int = 1, direction: Direction = "both",
                     types: Sequence[str] | None = None, max_nodes: int = 2000, as_of: int | None = None) -> Subgraph: ...
    def paths(self, org_id: str, from_id: str, to_id: str, *, max_depth: int = 6,
              types: Sequence[str] | None = None, limit: int = 10, as_of: int | None = None) -> list[Path]: ...

    # ---- aggregates
    def summary(self, org_id: str, *, as_of: int | None = None) -> Summary: ...
    def count_nodes(self, org_id: str, q: NodeQuery, *, group_by: Sequence[str] = (), as_of: int | None = None) -> list[Count]: ...

    # ---- writes: one scan at a time
    def begin_scan(self, org_id: str, *, scopes: Sequence[tuple[str, str]], job_id: str | None = None) -> ScanWriter: ...

    # ---- history
    def list_scans(self, org_id: str, *, limit: int = 50) -> list[Scan]: ...
    def diff(self, org_id: str, from_scan: int, to_scan: int) -> Diff: ...
    def prune(self, org_id: str, *, keep_days: int, keep_scans: int) -> int: ...

    # ---- compatibility, removed when the last caller is gone
    def fetch_graph(self, org_id: str, *, limit: int | None = None) -> dict: ...
```

Supporting types (dataclasses in `infra_map/graph_store.py`):

```python
Direction = Literal["incoming", "outgoing", "both"]

@dataclass(frozen=True)
class NodeQuery:
    types: Sequence[str] = ()
    regions: Sequence[str] = ()
    accounts: Sequence[str] = ()
    sources: Sequence[str] = ()
    ids: Sequence[str] = ()
    text: str | None = None          # name or id contains; FTS later
    prop_equals: Mapping[str, Any] = field(default_factory=dict)   # props->>'$.coverage_status' = 'unmanaged'
    changed_since: int | None = None  # valid_from > ts

@dataclass(frozen=True)
class EdgeQuery:
    types: Sequence[str] = ()
    from_ids: Sequence[str] = ()
    to_ids: Sequence[str] = ()
    sources: Sequence[str] = ()

@dataclass
class Page(Generic[T]):
    items: list[T]
    next_cursor: str | None
    total: int | None             # filled when cheap (a COUNT on the same WHERE), else None

@dataclass
class Subgraph:
    focus: str
    nodes: list[ResourceNode]
    edges: list[RelationshipEdge]
    truncated: bool               # max_nodes hit; the frontier was cut

@dataclass
class Path:
    nodes: list[str]
    edges: list[RelationshipEdge]

@dataclass
class Scan:
    scan_id: int; org_id: str; started_at: int; finished_at: int | None
    status: Literal["running", "completed", "failed", "aborted"]
    scopes: list[tuple[str, str]]; node_count: int; edge_count: int
    added: int; changed: int; removed: int; job_id: str | None

@dataclass
class Summary:
    scan: Scan | None
    nodes: int; edges: int
    by_type: dict[str, int]; by_region: dict[str, int]; by_account: dict[str, int]
    by_source: dict[str, int]; edge_types: dict[str, int]
```

### 3.3 Semantics

- **Scope key.** Everything is keyed by `(workspace_id, org_id)`: the store
  instance carries the workspace, every method takes `org_id`. This matches
  today's keys and leaves 1.8 room (an org per account set).
- **Point reads** return the current row (`valid_to IS NULL`) unless `as_of`
  is given, then the row valid at that millisecond.
- **`edges_of`** is bounded by `limit` and never truncates silently: the
  result is a list with an attribute, or the method raises `TooManyEdges`
  when the bound is hit and the caller did not ask for a bound. The API maps
  that to a 400 that says to narrow by type.
- **`neighborhood`** is a bounded BFS done in SQL (section 5); `truncated`
  says when `max_nodes` cut the frontier. Nodes are the union of what the BFS
  reached plus the focus; edges are every edge between those nodes, not only
  the tree edges.
- **`paths`** returns up to `limit` shortest paths up to `max_depth`,
  outgoing direction, over the given edge types. It is what 1.7 will call
  with `can_read`/`can_assume`; today it answers "how does this function reach
  that bucket".
- **`summary`** is one query per dimension over the current rows; counts are
  exact.
- **Time** is epoch milliseconds, integer, everywhere in the interface. ISO
  strings exist only at the API edge.

### 3.4 Transactions and how a scan writes

```python
with store.begin_scan(org_id, scopes=[("aws_scan", account), ("github", repo), ...], job_id=job_id) as scan:
    scan.put_nodes(nodes)        # any number of calls, any size
    scan.put_edges(edges)
# __exit__ with no exception: scan.commit(); with one: scan.abort()
```

`ScanWriter` semantics, the same for every backend:

1. `begin_scan` records a `scans` row with `status=running` and `started_at`
   = the scan's timestamp `T`. Only one running scan per `(workspace, org)`:
   a second `begin_scan` raises `ScanInProgress` unless the running one is
   older than `stale_after` (default 2 hours), in which case it is marked
   `aborted` and the new one proceeds. This is the daemon's "no heartbeat for
   three minutes" rule applied to the store.
2. `put_nodes` / `put_edges` buffer and upsert in batches (SQLite: 1000 rows
   per statement, `executemany`; DynamoDB: `batch_write_item` of 25).
   For each incoming row with id `k` in one of the scan's scopes:
   - no current row: insert with `valid_from = T`, `valid_to = NULL`,
     `first_seen = T`, `last_seen = T`;
   - current row with the same `content_hash`: `last_seen = T`, nothing else
     (unchanged rows cost one small update and no history row);
   - current row with a different hash: set its `valid_to = T`, insert the
     new row with `valid_from = T`.
   `content_hash` is the SHA-1 of the canonical JSON of the row without
   `last_seen`, `first_seen` and validity columns.
3. `commit` closes the rows the scan did not touch: every current row whose
   `(source, scope)` is among the scan's scopes and whose `last_seen < T`
   gets `valid_to = T`. Then the `scans` row gets `finished_at`, counts, and
   `status=completed`. Readers never see a half-written scan: in SQLite the
   whole scan is one transaction (section 4.3); in DynamoDB, which has no
   transaction of that size, the `scans` row is written last and readers of
   history use the last completed scan as "now".
4. `abort` rolls back (SQLite) or marks the scan `aborted` and leaves rows
   with `last_seen = T` in place (DynamoDB; the next scan repairs them).

The mapper's write block (`aws_mapper.py:198-215`) becomes the `with` above.
`fetch_graph` before the write, the stale sets and the deletes go away. The
fact store's `replace_scope` goes away with them: `source` and `scope` are
already columns on every node and edge, which is all the facts carried that
the graph did not. (Open decision 6.)

### 3.5 Stale rows once history exists

"Stale" stops meaning "delete". A row the scan did not see is *closed*: it
keeps its data and gets a `valid_to`. Current reads exclude it; `as_of` reads
before `valid_to` include it; `diff` lists it under `removed`. Closed rows
age out by retention (section 6.4). An edge is closed when the scan did not
produce it, whether or not its endpoints survived; an edge whose endpoint was
closed is therefore closed too, by the same rule, and nothing has to cascade.

## 4. SQLite as the open default store

### 4.1 Why SQLite

One file, in the standard library, no service, no new dependency, transactions
big enough to hold a scan, recursive CTEs for the graph queries, JSON for the
properties, and a `.backup` that is one copy. It is the right size for an
install that runs on one machine with one account, which is what the open
edition is. A larger deployment registers a Postgres (or Neptune, or AGE)
factory. Open decision 1 compares the alternatives.

### 4.2 Schema

`infra_map/sqlite_store.py`; schema version in `meta`, migrated in place by
the store on open (a `meta.schema` bump plus `ALTER`/rebuild steps, the way
`compass migrate` does for tables).

```sql
PRAGMA journal_mode = WAL;          -- set once, persisted in the file
PRAGMA synchronous = NORMAL;        -- durable at checkpoint; a crash loses at most the last transaction
PRAGMA foreign_keys = OFF;          -- validity is by convention, not FK; closed endpoints are legal
PRAGMA busy_timeout = 10000;

CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);     -- schema version, created_by

CREATE TABLE scans (
    scan_id      INTEGER PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    org_id       TEXT NOT NULL,
    started_at   INTEGER NOT NULL,          -- T, epoch ms; the validity boundary
    finished_at  INTEGER,
    status       TEXT NOT NULL,             -- running | completed | failed | aborted
    scopes       TEXT NOT NULL,             -- JSON [[source, scope], ...]
    job_id       TEXT,
    node_count   INTEGER, edge_count INTEGER,
    added        INTEGER, changed INTEGER, removed INTEGER,
    error        TEXT
);
CREATE INDEX scans_org ON scans (workspace_id, org_id, started_at DESC);

CREATE TABLE nodes (
    row_id       INTEGER PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    org_id       TEXT NOT NULL,
    node_id      TEXT NOT NULL,
    type         TEXT NOT NULL,
    name         TEXT NOT NULL DEFAULT '',
    provider     TEXT NOT NULL DEFAULT 'aws',
    account      TEXT NOT NULL DEFAULT '',
    region       TEXT NOT NULL DEFAULT '',
    source       TEXT NOT NULL DEFAULT '',
    scope        TEXT NOT NULL DEFAULT '',
    node_class   TEXT,
    confidence   TEXT NOT NULL DEFAULT 'high',
    props        TEXT NOT NULL DEFAULT '{}', -- JSON: tags, attributes, aliases, canonical_ref, terraform
    content_hash TEXT NOT NULL,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    valid_from   INTEGER NOT NULL,          -- scans.started_at of the scan that produced this version
    valid_to     INTEGER                    -- NULL = current
);
-- the current graph: at most one open version per node
CREATE UNIQUE INDEX nodes_current ON nodes (workspace_id, org_id, node_id) WHERE valid_to IS NULL;
CREATE INDEX nodes_current_type   ON nodes (workspace_id, org_id, type, name) WHERE valid_to IS NULL;
CREATE INDEX nodes_current_region ON nodes (workspace_id, org_id, region) WHERE valid_to IS NULL;
CREATE INDEX nodes_current_scope  ON nodes (workspace_id, org_id, source, scope, last_seen) WHERE valid_to IS NULL;
-- history: versions of one node, and what a scan added or closed
CREATE INDEX nodes_history ON nodes (workspace_id, org_id, node_id, valid_from);
CREATE INDEX nodes_valid_from ON nodes (workspace_id, org_id, valid_from);
CREATE INDEX nodes_valid_to   ON nodes (workspace_id, org_id, valid_to) WHERE valid_to IS NOT NULL;

CREATE TABLE edges (
    row_id       INTEGER PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    org_id       TEXT NOT NULL,
    edge_id      TEXT NOT NULL,             -- sha1(from->to|type)
    from_id      TEXT NOT NULL,
    to_id        TEXT NOT NULL,
    type         TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT '',
    scope        TEXT NOT NULL DEFAULT '',
    confidence   TEXT NOT NULL DEFAULT 'medium',
    props        TEXT NOT NULL DEFAULT '{}', -- JSON: evidence, attributes
    content_hash TEXT NOT NULL,
    first_seen   INTEGER NOT NULL,
    last_seen    INTEGER NOT NULL,
    valid_from   INTEGER NOT NULL,
    valid_to     INTEGER
);
CREATE UNIQUE INDEX edges_current ON edges (workspace_id, org_id, edge_id) WHERE valid_to IS NULL;
CREATE INDEX edges_current_from ON edges (workspace_id, org_id, from_id, type) WHERE valid_to IS NULL;
CREATE INDEX edges_current_to   ON edges (workspace_id, org_id, to_id, type)   WHERE valid_to IS NULL;
CREATE INDEX edges_current_type ON edges (workspace_id, org_id, type)          WHERE valid_to IS NULL;
CREATE INDEX edges_current_scope ON edges (workspace_id, org_id, source, scope, last_seen) WHERE valid_to IS NULL;
CREATE INDEX edges_history    ON edges (workspace_id, org_id, edge_id, valid_from);
CREATE INDEX edges_valid_from ON edges (workspace_id, org_id, valid_from);
CREATE INDEX edges_valid_to   ON edges (workspace_id, org_id, valid_to) WHERE valid_to IS NOT NULL;

-- name and id search for the dashboard's search box (FTS5 is in Debian's libsqlite3)
CREATE VIRTUAL TABLE nodes_fts USING fts5(node_id, name, content='nodes', content_rowid='row_id', tokenize='unicode61 tokenchars ":/-_."');
-- kept in step by triggers on nodes insert/delete; only current rows are searched (join back and filter)
```

Why `workspace_id` is a column when one file holds one workspace: the file
*may* hold several (a host that keeps one file per tenant group, or the
benchmark), and it costs nothing in the partial indexes. One org per workspace
is the open default; both keys are still in every index so the plan never
scans across orgs.

`props` is JSON text, read with `->>` only in filters the API exposes
(`coverage_status`, `drift_status`, `terraform.repo`). Anything filtered often
gets a generated column and an index at that point, not before.

Size: a node row is ~1 KB with typical attributes, an edge ~300 B. 100k nodes
and 350k edges ≈ 200 MB plus indexes ≈ 350 MB; with history, growth per scan
is the churn only (unchanged rows are a timestamp update), so a daily scan at
2% churn adds ~7 MB a day.

### 4.3 WAL, locking, the two containers

- The file is opened by the API (reads), the daemon (reads and the scan
  write), the CLI (reads; writes for an inline `mapper run`), and
  `compass migrate`. WAL mode lets readers proceed during the write and the
  write proceed during reads. Writers serialise on the file lock;
  `busy_timeout` makes a second writer wait rather than fail, and the store's
  own `ScanInProgress` rule (section 3.4) keeps two scans from queueing.
- **One transaction per scan.** `commit` is the only point where the new
  graph becomes visible; readers in the middle of a scan see the previous
  one, whole. The WAL grows to the size of the scan's writes (hundreds of MB
  at 1M nodes) and is checkpointed after commit (`PRAGMA
  wal_checkpoint(TRUNCATE)` by the writer). At 1M the alternative is chunked
  transactions with readers pinned to `scans.finished_at IS NOT NULL`;
  section 10 has the budget that decides.
- **Where the file lives.** A named Docker volume, `map-data`, mounted at
  `/compass/data` in both containers, like `dynamodb-data` for DynamoDB. Not
  a bind mount: WAL needs coherent shared memory for the `-shm` file and
  POSIX locks that work, which a named volume (ext4 inside the Docker VM)
  guarantees and a bind mount through Docker Desktop's virtiofs does not
  reliably. The path is `COMPASS_DATA_DIR` (default `/compass/data` in the
  image, `./data` in a checkout) and the file is
  `<COMPASS_DATA_DIR>/map/<workspace_id>.sqlite`. The `./map` bind mount stays
  what it is: the HTML export.
- **Connections.** One `sqlite3.Connection` per thread (the API is threaded
  under uvicorn), `check_same_thread=False` not used; a tiny per-process pool
  keyed by thread id, closed on shutdown through `compass_api.extensions`'
  shutdown hook. `isolation_level=None` and explicit `BEGIN IMMEDIATE` for the
  scan so the writer takes the lock up front and never deadlocks a reader
  that upgrades.
- **Durability.** `synchronous=NORMAL` in WAL mode: a power loss can lose the
  last transaction, which is one scan, which the next scan redoes. Not a
  problem worth `FULL`'s cost.

### 4.4 Backup and restore

- `compass mapper backup out=<path>`: `VACUUM INTO`, a consistent copy while the
  stack runs, without blocking readers.
- Restore is a file copy with the stack stopped: put the copy at
  `/compass/data/map/<workspace>.sqlite` in the volume and start it. A restore
  command that swaps the file under a running daemon was left out: the daemon
  and the API hold connections to the old file, and stopping them is what makes
  the swap safe.
- The updater's rollback does not touch the file: the schema only ever adds,
  and the version rolled back to reads DynamoDB until it is migrated again.

### 4.5 Implementation notes (what the code does where it differs from the text above)

- **The neighbourhood walks level by level**, one bounded query per level with the
  frontier as the `IN` list (`SqlGraphStore.neighborhood`), rather than the one
  recursive CTE of section 5. Same result, and a hub's fifty thousand edges cost
  one capped query (`max_nodes * 8` rows) instead of a frontier the CTE cannot
  bound. The CTE stays as the documentation of the shape; `paths` is a CTE.
- **`induced`**: `neighborhood(..., induced=True)` adds every edge between the
  reached nodes; the default returns the edges the walk crossed, which is what
  `GET /api/maps?focus=` always returned and what the DynamoDB backend's
  `neighbors` does. The new `/api/maps/neighbors` asks for `induced`.
- **The scan writes through temp tables**: rows land in `incoming_nodes` /
  `incoming_edges` (indexed, analysed), and four set operations do the rest:
  touch unchanged, close changed, insert new and changed versions, close unseen.
  On Postgres the rows arrive with `COPY`; on SQLite with `executemany`.
- **`first_seen` is the collector's observation time** (`last_seen` of the row as
  put), carried over from the oldest earlier version when there is one, so a
  migrated row keeps the date the old map recorded and a resource that
  disappears and comes back keeps its first sighting. `last_seen` and
  `valid_from` are the scan's `T`.
- **Three backends, one `SqlGraphStore`** with a dialect object: `SqliteGraphStore`
  and `PostgresGraphStore` differ in placeholders, the serial type, JSON
  operators and the bulk-insert path. Postgres needs `pip install
  compass[postgres]` (psycopg); it is the comparison point the benchmark asks
  for and the backend a hosted deployment would register, not the open default.
- **Which scan saw a row is its own column, `seen_scan`.** `last_seen` is when the
  collector observed the row, which is what the resolvers report as freshness;
  closing what a scan did not see compares `seen_scan`, not the clock. The
  upgrade rehearsal found the first version stamping `last_seen` with the
  scan's time, which made a migrated three-day-old map look fresh.
- **Numbers stay numbers.** DynamoDB returns them as `Decimal`; the store
  encodes those as integers or floats. The rehearsal found migrated coverage
  scores stored as strings, which broke `/api/iac/coverage`.
- **The fact store stays** in the interface PR (section 14, PR 3) and goes in its
  own PR (decision 6).
- **The world is denser in hubs than in edges**: 1.62 edges per node (26k edges
  for 16k nodes at 10k resources), with the busiest role at 1,659 incoming
  edges at 10k and 16,589 at 100k. Section 10.1's 3.4 was an estimate.

## 5. Example queries

All over current rows (`valid_to IS NULL`); the `as_of` variants replace that
predicate with `valid_from <= :t AND (valid_to IS NULL OR valid_to > :t)`.
`:ws`, `:org` are bound everywhere.

**Neighbourhood, depth 2, both directions, bounded.**

```sql
WITH RECURSIVE frontier(node_id, depth) AS (
    SELECT :focus, 0
    UNION
    SELECT CASE WHEN e.from_id = f.node_id THEN e.to_id ELSE e.from_id END, f.depth + 1
    FROM frontier f
    JOIN edges e ON e.workspace_id = :ws AND e.org_id = :org AND e.valid_to IS NULL
                AND (e.from_id = f.node_id OR e.to_id = f.node_id)
                AND (:types_empty OR e.type IN (SELECT value FROM json_each(:types)))
    WHERE f.depth < :depth
),
reached AS (SELECT node_id, MIN(depth) AS depth FROM frontier GROUP BY node_id ORDER BY depth, node_id LIMIT :max_nodes)
SELECT n.* FROM nodes n JOIN reached r ON n.node_id = r.node_id
WHERE n.workspace_id = :ws AND n.org_id = :org AND n.valid_to IS NULL;
-- then the edges between reached nodes:
SELECT e.* FROM edges e
WHERE e.workspace_id = :ws AND e.org_id = :org AND e.valid_to IS NULL
  AND e.from_id IN (SELECT node_id FROM reached) AND e.to_id IN (SELECT node_id FROM reached);
```

`truncated` is `(SELECT COUNT(*) FROM (SELECT DISTINCT node_id FROM frontier)) > :max_nodes`.
The recursion is bounded by `depth` (max 6 at the API) and, for hubs, by a
per-level cap the store applies by materialising `frontier` in a temp table
level by level when `depth > 2` (one statement per level, same CTE shape),
so a role with 50k incoming edges never fans out past `max_nodes`.

**k-hop, one direction, one edge type (what does this function write to,
transitively).**

```sql
WITH RECURSIVE walk(node_id, depth, path) AS (
    SELECT :from, 0, :from
    UNION ALL
    SELECT e.to_id, w.depth + 1, w.path || ' > ' || e.to_id
    FROM walk w JOIN edges e ON e.from_id = w.node_id AND e.type = :type
     AND e.workspace_id = :ws AND e.org_id = :org AND e.valid_to IS NULL
    WHERE w.depth < :k AND instr(w.path, e.to_id) = 0          -- no cycles
)
SELECT node_id, MIN(depth) AS depth FROM walk WHERE depth > 0 GROUP BY node_id;
```

**Shortest paths from A to B over a set of edge types (the 1.7 shape).**

```sql
WITH RECURSIVE walk(node_id, depth, path, edge_ids) AS (
    SELECT :from, 0, json_array(:from), json_array()
    UNION ALL
    SELECT e.to_id, w.depth + 1, json_insert(w.path, '$[#]', e.to_id), json_insert(w.edge_ids, '$[#]', e.edge_id)
    FROM walk w JOIN edges e ON e.from_id = w.node_id
     AND e.workspace_id = :ws AND e.org_id = :org AND e.valid_to IS NULL
     AND e.type IN (SELECT value FROM json_each(:types))
    WHERE w.depth < :max_depth AND w.node_id != :to
      AND NOT EXISTS (SELECT 1 FROM json_each(w.path) WHERE value = e.to_id)
)
SELECT path, edge_ids, depth FROM walk WHERE node_id = :to ORDER BY depth LIMIT :limit;
```

**Counts by type within a scope (the summary, and the IaC coverage card).**

```sql
SELECT type, COUNT(*) AS total,
       SUM(CASE WHEN CAST(props ->> '$.attributes.coverage_score' AS REAL) >= 1.0 THEN 1 ELSE 0 END) AS covered,
       SUM(CASE WHEN props ->> '$.attributes.drift_status' = 'drifted' THEN 1 ELSE 0 END) AS drifted
FROM nodes
WHERE workspace_id = :ws AND org_id = :org AND valid_to IS NULL AND source = 'aws_scan'
GROUP BY type ORDER BY total DESC;
```

**Ownership (what the resolver and the librarian walk today in Python):
resource ← observes ← tf_state_instance → contains ← tf_state_file ← writes ← tf_root_module → part_of → repo.**
This is `paths(from=resource, to=*, types=[observes⁻¹, ...])`; the resolver
keeps its scoring in Python but gets the candidate paths from one query
bounded at depth 4, instead of the whole graph.

**Page of nodes, keyset.**

```sql
SELECT * FROM nodes
WHERE workspace_id = :ws AND org_id = :org AND valid_to IS NULL
  AND (:types_empty OR type IN (SELECT value FROM json_each(:types)))
  AND (:region IS NULL OR region = :region)
  AND (type, name, row_id) > (:c_type, :c_name, :c_row)         -- the cursor
ORDER BY type, name, row_id LIMIT :limit + 1;                    -- +1 tells us whether there is a next page
```

## 6. History

### 6.1 Model

Every node and edge row is a *version* with `[valid_from, valid_to)`. A scan
at `T` opens versions at `T` and closes versions at `T`. There is no separate
history table: the current graph is the partial index over `valid_to IS NULL`,
so current reads never pay for history. `first_seen` and `last_seen` live on
the version; a node's first appearance ever is `MIN(valid_from)` over its
versions, which `as_of`/`diff` compute and the node detail shows.

### 6.2 "As of"

`as_of=T'` on any read means: the row with `valid_from <= T' < valid_to`
(or `valid_to IS NULL`). Resolution is a scan boundary: `T'` between two scans
sees the earlier one. The API accepts `as_of` as an ISO time or a `scan_id`
(resolved to that scan's `started_at`); the dashboard offers a scan picker
only, which is what people mean by "yesterday's map".

### 6.3 Diff between scans

```sql
-- added or changed in scan B (started_at = :tb)
SELECT 'node' AS kind, node_id, CASE WHEN EXISTS (SELECT 1 FROM nodes p WHERE p.node_id = n.node_id AND p.valid_to = :tb) THEN 'changed' ELSE 'added' END AS change
FROM nodes n WHERE n.workspace_id = :ws AND n.org_id = :org AND n.valid_from = :tb
UNION ALL
-- removed in scan B: closed at B and not reopened at B
SELECT 'node', node_id, 'removed' FROM nodes p
WHERE p.workspace_id = :ws AND p.org_id = :org AND p.valid_to = :tb
  AND NOT EXISTS (SELECT 1 FROM nodes n WHERE n.node_id = p.node_id AND n.valid_from = :tb);
```

The same for edges. `diff(A, B)` with scans in between walks the boundaries
from A to B and folds (added then removed = nothing; changed twice = changed,
with the first and last versions). For a `changed` node the API returns both
versions' `props` and the list of changed keys (computed in Python from the
two JSON documents; it is two rows, not a graph).

### 6.4 Retention

Closed versions are kept for `map.history_days` (default **90**) and never
fewer than the last `map.history_scans` (default **10**) scan boundaries, so a
workspace scanned hourly keeps 90 days and one scanned monthly keeps ten
months. `prune` deletes closed rows whose `valid_to` is older than both rules
allow and drops their `scans` rows; it runs in the daemon once a day after the
first scan and is `compass map prune` from the shell. Current rows are never
pruned. A node that disappears and comes back inside the window is a new
version with the old `first_seen` preserved in the API through
`MIN(valid_from)`. Open decision 3 has the alternatives.

## 7. The query API

Everything is under `/api/maps/…`, gated like the rest of `/api/`. Node ids are
ARNs with colons and slashes, so a node is addressed by a query parameter,
never a path segment. Times are ISO-8601 in and out; `as_of` also accepts a
`scan_id`. Every list is paged with an opaque `cursor`; `limit` has a default
and a hard cap; nothing is cut without saying so.

| Endpoint | Purpose | Parameters | Returns |
|---|---|---|---|
| `GET /api/maps/summary` | first thing the dashboard loads | `org_id`, `as_of` | `{scan, nodes, edges, by_type, by_region, by_account, by_source, edge_types}` |
| `GET /api/maps/nodes` | a page of nodes | `org_id`, `type[]`, `region[]`, `account[]`, `source[]`, `q` (name/id contains), `coverage` (`covered|unmanaged|drifted`), `changed_since`, `as_of`, `cursor`, `limit` (default 500, max 5000) | `{items, next_cursor, total}` |
| `GET /api/maps/node` | one node with its degree | `org_id`, `id`, `as_of` | `{node, in_degree, out_degree, degree_by_type, first_seen, versions}` or 404 |
| `GET /api/maps/edges` | a page of edges | `org_id`, `type[]`, `from`, `to`, `source[]`, `as_of`, `cursor`, `limit` (default 1000, max 10000) | `{items, next_cursor, total}` |
| `GET /api/maps/neighbors` | the subgraph around a node | `org_id`, `id`, `depth` (1..6, default 1), `direction`, `type[]`, `max_nodes` (default 500, max 2000), `as_of` | `{focus, nodes, edges, truncated}` |
| `GET /api/maps/paths` | shortest paths between two nodes | `org_id`, `from`, `to`, `type[]`, `max_depth` (1..8, default 6), `limit` (default 5, max 25) | `{paths: [{nodes, edges}]}` |
| `GET /api/maps/scans` | scan history | `org_id`, `limit` | `{items: [scan]}` |
| `GET /api/maps/diff` | what changed between scans | `org_id`, `from` (scan_id), `to` (scan_id, default latest), `kind`, `cursor`, `limit` | `{from, to, items: [{kind, id, change, before, after, changed_keys}], next_cursor}` |
| `GET /api/maps` | unchanged, compatibility | as today | as today, plus `truncated: bool` and `total_nodes`, `total_edges` |

Shapes of a node and an edge in responses are today's `_serialize_node` /
`_serialize_edge` dicts (`maps.py:36-46`), plus `provider`, `first_seen`,
`valid_from`, `valid_to` (null when current). Nothing the CLI's `map get`
reads is renamed.

**Pagination.** Keyset: the cursor is base64 of the last row's sort tuple;
the sort is fixed per endpoint (`type, name, row_id` for nodes; `type,
from_id, to_id, row_id` for edges; `change, kind, id` for diff). A cursor from
a different filter is refused with 400. `total` is the exact count for the
filter, computed with the first page only (the response for a cursor page
has `total: null`).

**Limits and errors.**

| Condition | Status | `detail` |
|---|---|---|
| unknown node in `node`, `neighbors`, `paths` | 404 | `no node <id> in the map` |
| `limit` or `depth` outside the cap | 422 (FastAPI validation) | |
| bad or mismatched cursor | 400 | `cursor does not belong to this query` |
| `neighbors` frontier over `max_nodes` | 200 with `truncated: true` | |
| `edges_of` over 5000 on one node inside `neighbors` | 200, `truncated: true` | |
| store has no completed scan | 200 with `scan: null` and empty counts | |
| `as_of` before the oldest retained scan | 400 | `history starts at <date>` |
| a scan is being written (SQLite busy past `busy_timeout`) | 503 | `the map is being written; retry` |

**GraphQL.** Not now. Four clients (dashboard, CLI/MCP, the captains, the
export) need five fixed shapes; a GraphQL layer would add a dependency
(strawberry or ariadne), a schema to keep in step with the models, and
per-field resolvers that reintroduce the N+1 reads over SQLite that this spec
removes. The point at which it earns its place is a third-party client that
needs shapes we did not anticipate; the paged endpoints above are designed so
that can be added on top without changing the store.

## 8. The dashboard contract

Only what the API must support. The map's look, combos and overlays stay.

1. **First load:** `GET /api/maps/summary`. It renders the scan state, the
   counts by type and region, the type palette with counts, and the search
   box. No graph yet.
2. **Small maps draw whole.** If `summary.nodes <= 3000` (`MAP_DRAW_ALL`
   constant), the tab pages through `/api/maps/nodes` and `/api/maps/edges`
   (limit 5000) and draws everything, as today. This keeps the e2e suite's
   seven-resource world and most first installs looking exactly as they do.
3. **Large maps draw on demand.** Above the threshold the canvas starts
   empty with the palette and a line "N resources; search, or pick a type to
   draw". Picking a type with a count under the threshold loads
   `nodes?type=…` plus the edges among them (`edges?type=…` filtered
   client-side to the loaded ids; one request per edge type the palette
   shows). Search calls `nodes?q=…` (server-side, paged) and lists matches;
   clicking one calls `neighbors?id=…&depth=1` and merges the subgraph into
   the canvas. Expand on a drawn node is the same call; the depth slider maps
   to `depth`, capped by `truncated`.
4. **Focus mode** uses `neighbors` instead of the client-side
   `computeSubgraph`; the client-side version stays for the small-map case.
5. **Overlays** (coverage, drift, activity) read what they read today from
   node props, nothing new.
6. **IaC tab** gets `summary` (for `coverage_pct` and `by_type`) and
   `nodes?coverage=unmanaged` / `nodes?coverage=drifted` paged, replacing
   `/api/iac/coverage`'s whole lists; `/api/iac/coverage` itself stays and is
   served from the same two queries. `/api/iac/suggestions` becomes
   `nodes?type=github_repo` and `nodes?type=tf_root_module`.

## 9. Migration from DynamoDB, and rollback

**Selection.** Workspace config gains `map: {"store": "sqlite" | "dynamodb"}`.
`init_config` writes `sqlite`. Absent means `dynamodb` until the migration
step has run, then `sqlite`: the step sets it. `COMPASS_MAP_STORE` overrides
for one process (the rollback switch, and the benchmark).

**The step**, registered as `("0.6.0-map-sqlite", step_0_6_0)` in
`compass_core.migrations.STEPS` (`infra_map/migrate.py`):

1. The target is the store the workspace names (`map.store`, or
   `COMPASS_MAP_STORE` for the process): Postgres for a deployment that set
   it, else the SQLite file, which is also what an install that names nothing
   (`dynamodb`, the pre-0.6 default) gets.
2. Open the target, create the schema. If it already holds a completed scan
   for the org (the daemon started first and scanned): record the store, done.
   The same when the DynamoDB map table is empty or there is none.
3. Read the DynamoDB graph with the old store, paginated, in one
   `begin_scan`; each row's `valid_from` is its `last_seen` (so "first_seen"
   is at least as old as the old map said), `commit`. The scan is marked
   `job_id = "migration:dynamodb"`.
4. Set `map.store` to the target's name (`sqlite`, or the `postgres` it
   already said) and save the config. The DynamoDB table is not touched: a
   migration step never deletes.

At 100k items this is the whole-graph read (section 10 measures it; tens of
seconds against DynamoDB-local) plus one SQLite transaction. It runs under the
extensions lock before `serve`/`api` start, so the first start after the
update is slower by that much, once. The entrypoint already tolerates a
migrate failure and starts anyway; in that case the config still says
`dynamodb` and nothing has changed for the install.

**Rollback.** `COMPASS_MAP_STORE=dynamodb` in `.env`, or `map.store` back to
`dynamodb`: the old store code is still there and the table was never
deleted; the next scan rewrites it. Rolling back the image to a version
without this code is the same: that version only reads DynamoDB and the
`map` key is ignored. The SQLite file is left in place and the next forward
migration finds its completed scan and skips the copy. What is lost on
rollback is the history collected in the meantime, which the old version
could not show anyway.

**DynamoDB as a store after migration.** The `DynamoGraphStore` implements the
interface fully (history included, with `valid_to` as an attribute and a
GSI on it) *or* only the current-graph subset with history methods raising
`NotImplementedError("history needs the sqlite store")`. Open decision 2.

## 10. The benchmark

`tests/benchmark/`: a package the unit tests import, `pytest -m benchmark`
runs, and CI calls with a scale argument. Nothing in it reaches the network;
DynamoDB is moto (in CI) or DynamoDB-local in Docker (locally, closer to what
an install runs).

### 10.1 The synthetic world (`infra_map/synthetic.py`, until 0.8.0 `tests/benchmark/world.py`)

Deterministic from a seed; parameters `resources`, `regions` (default 4),
`managed_share` (0.6), `churn` (0.05, for the second scan). The mix per 1000
resources follows what the collectors produce and what a real account looks
like:

| Type | per 1000 | Edges it produces |
|---|---|---|
| `ec2_instance` | 150 | `secured_by` → 1-3 security groups, `member_of` → subnet-less (none), `attached_to` ← network interface |
| `ec2_network_interface` | 150 | `attached_to` → instance or lambda |
| `ec2_security_group` | 100 | referenced; in-degree power-law (a few groups with thousands) |
| `lambda_function` | 120 | `uses_in` → role, `secured_by` → groups, `writes_to` → log group, `depends_on` → queue/topic/table (0-2) |
| `cloudwatch_log_group` | 130 | |
| `iam_role` | 60 | in-degree power-law: 5 roles carry half the `uses_in` edges |
| `s3_bucket` | 40 | `depends_on` ← lambda |
| `sqs_queue`, `sns_topic` | 35, 25 | `routes_to` topic → queue |
| `dynamodb_table` | 30 | |
| `secretsmanager_secret` | 25 | `uses_in` ← task definition, `secured_by` → kms (prop) |
| `ecs_cluster`, `ecs_service`, `ecs_task_definition` | 3, 20, 25 | `member_of`, `uses_in`, `exposes` |
| `elbv2_load_balancer`, `elbv2_target_group` | 8, 12 | `routes_to` |
| `rds_instance` | 12 | `secured_by` |
| `eventbridge_rule`, `cloudwatch_alarm`, `stepfunctions_state_machine`, `ecr_repository` | 15, 20, 5, 15 | `routes_to`, `depends_on` |

Ownership nodes: one `github_repo` per 2500 resources, one `tf_root_module`
per 200, one `tf_state_file` per root module, one `tf_state_instance` per
managed resource (`managed_share`), with `observes`, `contains`, `writes`,
`part_of`, `corresponds_to` edges, and coverage/drift props set the way
`coverage_drift.py` sets them (3% drifted). Resulting density: about 3.4
edges per node, so 100k resources is ~160k nodes and ~540k edges. Regions are
spread 50/25/15/10. Names, tags and attributes are realistic in size (a node
serialises to ~900 B).

Scale points: 10k, 100k, 1M resources. The second scan of each is the same
world with `churn` nodes removed, as many added, and 2% of props changed,
which is what a daily scan looks like.

### 10.2 Metrics

For each store and scale, in a fresh process, with `tracemalloc` off and
`resource.getrusage` for peak RSS; every measurement starts after a full
garbage collection, so a pass over the worlds the process holds never lands
inside a read:

| Metric | What is measured |
|---|---|
| `scan_first_s` | `begin_scan` to `commit` for the first world, wall clock, in the daemon's shape (the mapper's write block only; collection is not measured) |
| `scan_second_s` | the same for the churned world (the stale pass is where today's path spends its time) |
| `fetch_graph_s` | today's whole-graph read; kept as the migration's cost |
| `first_load_s`, `first_load_bytes` | the dashboard contract's first load: `summary` plus, under the threshold, the full page-through; above it, `summary` only |
| `neighborhood_hub_ms` | `neighbors(depth=2)` of the biggest role, `max_nodes=500` |
| `neighborhood_ms` | `neighbors(depth=2)` of a median instance |
| `page_ms` | one `query_nodes` page of 500 with a type filter |
| `summary_ms` | `summary` |
| `ownership_ms` | the resolver's question for one managed resource |
| `paths_ms` | `paths` lambda → bucket, depth 4 |
| `peak_rss_mb` | the process over the whole run |
| `db_size_mb` | file size (SQLite) or item count × average size (DynamoDB, reported) |

### 10.3 Proposed budgets

Fail CI when exceeded; the 1M column is a target, reported, not enforced
until measured on the CI runner.

| Metric | 10k | 100k | 1M |
|---|---|---|---|
| `scan_first_s` | 10 | 60 | 900 |
| `scan_second_s` | 10 | 60 | 900 |
| `first_load_s` | 1.0 | 2.0 | 3.0 |
| `first_load_bytes` | 8 MB (draws whole) | 100 KB (summary) | 100 KB |
| `neighborhood_hub_ms` | 100 | 300 | 1000 |
| `neighborhood_ms` | 50 | 100 | 300 |
| `page_ms` | 50 | 100 | 300 |
| `summary_ms` | 100 | 500 | 3000 |
| `ownership_ms` | 50 | 100 | 300 |
| `paths_ms` | 100 | 300 | 1000 |
| `peak_rss_mb` (daemon, scan) | 300 | 1024 | 4096 |
| `db_size_mb` | 40 | 400 | 4000 |

For reference, today's path at 100k, as measured in Phase 2, is expected to
be minutes for the scan (400k+ sequential `put_item` calls plus the whole-graph
read and the delete pass) and seconds per request for every `fetch_graph`
caller; the numbers go in this section when they exist.

### 10.4 Where it runs

Open decision 5. Proposal:

- **Every pull request** that touches `infra_map/`, `captains/mapper/`,
  `compass_api/routes/maps.py` or `tests/benchmark/`: the 10k point, both
  stores, budgets enforced, as a job in `ci.yml` (about two minutes).
- **Nightly on `main`** (the e2e workflow's schedule): 100k, budgets
  enforced; the numbers are posted as a job summary and kept as an artifact.
- **On demand** (`workflow_dispatch` with `scale=1m`): 1M, reported only.
- Locally: `pytest -m benchmark --scale 100k --store sqlite,dynamodb`
  against DynamoDB-local in Docker; `docs/benchmark.md` says how.

### 10.5 Results (2026-10-19, this machine: M-series Mac, 17 GB, Docker Desktop with a 9.7 GB VM)

Each store ran alone except where noted; DynamoDB is DynamoDB-local in Docker
(`-inMemory`), Postgres 16 in Docker, SQLite in-process on the local disk.
"dynamodb (today)" is the code on `main` before this work; "dynamodb
(GraphStore)" is the same table behind the interface.

**10k resources: 16,104 nodes, 26,166 edges**

| metric | budget | dynamodb (today) | dynamodb (GraphStore) | sqlite | postgres |
|---|---|---|---|---|---|
| scan_first_s | 10.0 s | 110.9 s **over** | 123.3 s **over** | 1.1 s | 4.8 s |
| scan_second_s | 10.0 s | 152.7 s **over** | 150.8 s **over** | 1.0 s | 4.7 s |
| first_load_s | 1.0 s | 2.9 s **over** | 18.3 s **over** | 0.3 s | 0.2 s |
| first_load_bytes | 8.00 MB | 2.74 MB | 1 KB | 1 KB | 1 KB |
| neighborhood_hub_ms | 100 ms | 26542 ms **over** | 38227 ms **over** | 27 ms | 62 ms |
| neighborhood_ms | 50 ms | 1730 ms **over** | 11236 ms **over** | 40 ms | 143 ms **over** |
| page_ms | 50 ms | 33097 ms **over** | 23643 ms **over** | 4 ms | 8 ms |
| summary_ms | 100 ms | 20384 ms **over** | 18640 ms **over** | 12 ms | 32 ms |
| ownership_ms | 50 ms | 18041 ms **over** | 15967 ms **over** | 473 ms **over** | 572 ms **over** |
| paths_ms | 100 ms | - | 13521 ms **over** | 0 ms | 5 ms |
| peak_rss_mb | 300 MB | 233 MB | 230 MB | 285 MB | 257 MB |
| db_size_mb | 40 MB | 41 MB **over** | - | 47 MB **over** | 81 MB **over** |

**100k resources: 161,040 nodes, 261,461 edges**

| metric | budget | dynamodb (today) | sqlite | postgres |
|---|---|---|---|---|
| scan_first_s | 60.0 s | 1072.0 s **over** | 16.7 s | 34.9 s |
| scan_second_s | 60.0 s | - | 12.5 s | 62.3 s **over** |
| first_load_s | 2.0 s | - | 0.8 s | 4.2 s **over** |
| first_load_bytes | 100 KB | - | 1 KB | 1 KB |
| neighborhood_hub_ms | 300 ms | - | 78 ms | 551 ms **over** |
| neighborhood_ms | 100 ms | - | 106 ms **over** | 382 ms **over** |
| page_ms | 100 ms | - | 6 ms | 22 ms |
| summary_ms | 500 ms | - | 197 ms | 251 ms |
| ownership_ms | 100 ms | - | 6406 ms **over** | 11220 ms **over** |
| paths_ms | 300 ms | - | 1 ms | 9 ms |
| peak_rss_mb | 1024 MB | - | 1807 MB **over** | 1798 MB **over** |
| db_size_mb | 400 MB | - | 470 MB **over** | 809 MB **over** |

**Reading the numbers.**

- **Today's path does not reach 100k.** The first scan took 1072 s (18 minutes)
  against DynamoDB-local, and the read phase was stopped after two hours: once
  the table held 422k items, DynamoDB-local answered a single `get_item` in 2 to
  16 s, so the hub neighbourhood (about 33k queries on today's one-query-per-node
  walk) would have taken days, and `GET /api/maps?limit=4000`, the dashboard's
  first request, did not return within 15 minutes. That is DynamoDB-local in
  `-inMemory` mode with the JVM's default heap; the shipped compose runs it
  with `-dbPath` on a volume, which is slower per request, not faster. At 10k the
  same path is already over every budget except memory: 111 s and 153 s per
  scan, 20 to 33 s for each whole-graph read that coverage, suggestions and the
  resolvers do per request, 27 s for a hub neighbourhood.
- **The interface costs nothing on DynamoDB writes**: "dynamodb (GraphStore)" is
  the same table through `DynamoGraphStore`, and its scans are within 10% of
  "today". Its neighbourhoods are slower (38 s vs 27 s for the hub) because the
  adapter asks for the induced subgraph, every edge between the reached nodes,
  which on DynamoDB is one more query per node.
- **SQLite meets every budget it can meet at both scales**, and is the fastest
  store on every read. At 100k: scans of 17 s and 13 s (the second, with 5%
  churn, is cheaper because most rows only get `last_seen` touched); first
  load 0.8 s; summary 197 ms; a hub neighbourhood at depth 2 bounded to 500
  nodes 78 ms; a page 6 ms; paths 1 ms once `ANALYZE` has run (1.8 s before
  it, hence the `ANALYZE` after every scan).
- **Postgres is two to seven times slower than SQLite** on the same queries:
  each read is many small round trips over TCP to the container, and each
  scan maintains nine indexes per table. At 100k: scans of 35 s and 62 s,
  first load 4.2 s, hub neighbourhood 551 ms. Its first scan was quadratic
  until the insert stopped reading the table it was growing (section 4.5):
  4,542 s before the fix, 35 s after. Postgres is the backend for a hosted,
  multi-tenant deployment, not for the open edition: a container, a password,
  a driver, and slower here.
- **`first_load_s` is the first read after a scan**, with cold caches; the same
  summary a moment later is 197 to 251 ms. The dashboard will see the cold one
  right after a scan.
- **The DynamoDB `GraphStore` adapter's first load is slower than today's**
  (18 s vs 3 s at 10k) because its summary is a whole-graph read; today's first
  load is a cut-off read of 4,000 items. Neither is usable; the comparison that
  matters is with the SQL stores.
- **Four budgets are wrong, not the stores.** `neighborhood_ms` for a "typical"
  instance: a median instance's depth-2 neighbourhood goes through a hub and
  is 500 nodes, the cap; it costs what the hub costs. `ownership_ms` measures
  today's resolver, which loads the whole graph through `fetch_graph` on every
  store (6 to 11 s at 100k); PR 5 moves it to `paths`, which answers in 1 to
  9 ms here. `peak_rss_mb` is the benchmark process holding two 100k worlds
  as dataclasses plus the resolver's whole-graph copy, not the store; the
  mapper holds the collector's output the same way today. `db_size_mb`: 470 MB
  for SQLite at 100k (2.9 KB per node, 1.1 KB per edge with eight and nine
  indexes) and 809 MB for Postgres; the 400 MB guess should be 500 MB for
  SQLite, or two of the indexes should go.
- **The next optimisation**, if the second scan matters: the touch of every
  unchanged row's `last_seen` rewrites 400k rows (and their index entries, on
  Postgres) per scan. Deriving `last_seen` of a current row from the last
  completed scan covering its scope, instead of storing it per version, would
  make a scan cost its churn only; the resolver's freshness reads would take it
  from the `scans` table. Not done; the numbers do not need it yet.
- **Noise.** Repeated runs of the same store differ by up to 2x on this laptop
  (SQLite's 100k second scan: 13 s in the run above, 26 s in an earlier one
  that overlapped the DynamoDB run). Every number in the tables above is from a
  run alone, one run each; treat differences under 2x between runs as noise
  and the order-of-magnitude gaps as the result.

## 11. What changes where

**API.** The endpoints in section 7; `/api/maps` gains `truncated` and totals;
`/api/iac/coverage` and `/api/iac/suggestions` keep their shapes and are
served from paged queries; `/api/maps/ownership` and
`/api/librarian/terraform-location` unchanged in shape, served from `paths`.

**CLI (`cli/src/tools/map.ts`, `compass map`).** `get` keeps working and
gains `--as-of`. New actions: `summary`, `search <text>` (nodes `q=`),
`neighbors <id> [--depth]`, `paths <from> <to>`, `scans`, `diff [--from
--to]`. `compass_core.cli` gets `map backup | restore | prune` as mapper
commands (`captains/mapper/commands.py`), with the command table in
`tests/compass_core/test_core_cli.py` updated. The MCP tool description says
the map is paged and names the summary as the place to start.

**Skill (`.claude/skills/compass-open/SKILL.md`).** Step 5 ("the first map")
says where the map file lives, that `compass map summary` is the quick check,
and that the update migrates an existing map once on first start. The
`compass-status` plugin skill mentions scan history where it lists what the
status shows, if `compass status` gains the last scan (it should: one line).

**Docs.** `docs/architecture.md`: the Storage section says the map is a
SQLite file on the `map-data` volume, DynamoDB holds the rest, and names the
`GraphStore` seam in the seams table. `docs/quickstart.md`: the file's
location, backup, the migration on update, "Where things are". A new
`docs/map-queries.md`: the endpoints with examples (the API docs the CLI
links to). `docs/e2e.md`: the map tests that change. `CHANGELOG.md`: a
`## 0.6.0 — unreleased` section with an "After the update" bullet about the
one-time migration and the new volume. `README.md`: the "What is inside"
table's `infra_map` line.

**Compose.** `docker-compose.yml`: the `map-data` volume mounted at
`/compass/data` on both Compass services. The compose update test in e2e
covers that the volume appears.

## 12. Test strategy

- **Contract tests for the store** (`tests/infra_map/test_graph_store_contract.py`):
  one parametrised module run against every registered backend (`sqlite` on
  a temp file; `dynamodb` on moto). Scan write semantics (new, unchanged,
  changed, closed), `ScanInProgress`, point reads, paging with cursors across
  filters, neighbourhood bounds and `truncated`, paths, summary, `as_of`,
  diff folding, prune rules. These replace the `MagicMock` store tests.
- **SQLite specifics** (`tests/infra_map/test_sqlite_store.py`): WAL is on,
  two connections from two threads (one writing a scan, one reading) see the
  old graph until commit, `busy_timeout` behaviour, schema migration from
  version 1 to 2, backup and restore, FTS search.
- **Route tests pinned before the refactor** (`tests/api/test_maps_routes.py`):
  `/api/maps` whole and focused, `/api/iac/coverage`, `/api/iac/suggestions`,
  `/api/honeypots/suggestions`, `/api/honeypots/repos`, `/api/maps/ownership`,
  `/api/librarian/terraform-location` against a moto DynamoDB with a small
  seeded graph; the Phase 3 refactor must leave their responses byte-equal.
  Then the new endpoints in the same file, against both stores.
- **Mapper write path** (`tests/captains/mapper/test_aws_mapper.py`): the
  recording store becomes a recording `ScanWriter`; a test that a second run
  closes what the collector no longer returns and leaves other scopes alone.
- **Migration** (`tests/compass_core/test_migrations.py`): a moto map table
  with 500 items migrates into SQLite once, is skipped the second time, is
  skipped when the daemon scanned first, and leaves the table intact; the
  config flips; `COMPASS_MAP_STORE=dynamodb` reads the table again.
- **Dashboard**: `npx tsc --noEmit`; the e2e map tests (`e2e/test_05_map.py`)
  keep passing on the small world (draws whole), plus one e2e test that seeds
  the 10k world into the store directly (through the API container's
  `compass` command, not through moto) and checks the summary-first load and
  an expand.
- **Benchmark** as its own marker, excluded from `pytest -q` by
  `addopts = -m "not benchmark"` in `pyproject.toml`.

## 13. Risks

- **SQLite on a shared volume.** WAL across two containers is sound on a
  named volume; on a bind mount through Docker Desktop it is not guaranteed.
  Mitigation: the named volume is the default; the store checks
  `PRAGMA journal_mode` after open and logs a warning with the fix if WAL
  could not be set. An install that insists on a bind mount gets
  `journal_mode=DELETE` and readers blocked during a scan, which still works.
- **One transaction per scan at 1M.** WAL growth and commit time; the 1M
  budget decides whether chunked commits (readers pinned to the last
  completed scan) are needed. Designed in, not built.
- **Hub nodes.** A role with 50k `uses_in` edges makes depth-2 BFS fan out
  past any cap; the per-level materialisation and `max_nodes` bound it, and
  the benchmark's power-law in-degree is there to prove it.
- **Behaviour drift during the refactor.** The route tests pinned first make
  it checkable; the fact store's removal is the one place today's writes
  change (facts were never read), called out in the PR.
- **Migration time on first start.** Minutes at 100k against a real DynamoDB
  endpoint; the entrypoint starts anyway on failure and the install keeps
  running on DynamoDB. The changelog's "After the update" says so.
- **The dashboard threshold.** 3000 is a guess at what Graphin draws
  smoothly; the Phase 5 numbers set it.
- **Two writers.** An inline `compass mapper run` while the daemon scans:
  `ScanInProgress` refuses the second one with a one-line message, as the job
  queue refuses a duplicate scan today.
- **Disk.** History grows with churn; retention bounds it, and the summary
  reports the file size so it is visible before it is a problem.

## 14. PR plan

Each is shippable alone, in this order, with the stops you asked for.

1. **`tests: a synthetic map world and the map benchmark`** (1.10). The
   generator, the harness, the DynamoDB numbers at 10k and 100k against
   moto and DynamoDB-local, `docs/benchmark.md`, the CI job on demand only
   (nothing to enforce yet). *Stop: baseline numbers against the budgets.*
2. **`api: tests that pin the map routes`**. The route tests of section 12,
   green on today's code. Small; could fold into 3 but a refactor PR is
   easier to review when its tests pre-exist.
3. **`map: a GraphStore interface, the DynamoDB store behind it`** (1.1).
   The protocol, the registry, `DynamoGraphStore` wrapping today's code,
   every caller through `open_store` and the interface, the mapper writing
   through `ScanWriter` (still delete-based inside the DynamoDB backend, so
   behaviour is unchanged). The fact store stays in this PR. *Stop: diff
   summary, tests, benchmark unchanged.*
4. **`map: SQLite is the open store; existing maps migrate once`** (1.2).
   The SQLite store, the contract tests on both, the migration step and the
   config key, the compose volume, backup/restore commands, docs and the
   changelog. *Stop: before/after numbers at 10k and 100k. This is the
   performance review on the large fake account you asked for.*
5. **`api: paged map queries`** (1.3). The endpoints, `truncated` on
   `/api/maps`, coverage and suggestions on paged queries, the resolver and
   librarian on `paths`, CLI actions, skill, `docs/map-queries.md`.
6. **`dashboard: the map loads a summary first and draws on demand`**. The
   contract of section 8, nothing else. *Stop: the map at 100k, with numbers
   and a description of what the page shows.*
7. **`map: history`** (1.4). Versions were already written by 4; this PR
   adds `as_of`, `diff`, `scans`, retention and `prune`, the API and CLI
   for them, and the scan picker contract. *Stop.*
8. **`map: the fact store goes`**, if decision 6 says so: a one-page deletion
   on its own.

## Decisions taken (2026-10-19)

1. **SQLite is the open default.** Postgres stays as a backend behind the same
   interface, for a hosted deployment, through the optional `postgres` extra.
   Other free options were considered and not adopted: DuckDB (one writing
   process per file locks the other process out), Kùzu (an embedded graph
   database whose original project was archived), Neo4j Community, Apache AGE,
   JanusGraph and NebulaGraph (a server each, heavier than Postgres for the same
   queries), and the source-available Memgraph, ArangoDB, FalkorDB and SurrealDB.
2. **Budgets** revised from the first results: the typical neighbourhood takes the
   hub's budget, peak memory and file size are raised to what the stores need,
   and `ownership_ms` is reported, not enforced, until the query API moves the
   resolver to `paths`.
3. **The benchmark** runs at 10k on every pull request (`ci.yml`, budgets
   enforced, seconds) and at 100k nightly on SQLite (`benchmark.yml`), any scale
   and store on demand.

The rest of the list below stays open.

## Open decisions

**1. The open default store: SQLite, or Postgres (+ Apache AGE).**
Options: (a) SQLite in a file on a shared volume, as specified; (b) Postgres
as a fourth compose service, plain SQL with recursive CTEs; (c) Postgres with
the AGE extension and openCypher. *Recommendation: (a).* The open edition is
one machine and one account; SQLite adds no service, no dependency, no
operational surface, and the queries this spec needs run well in it to a few
million rows. (b) is the right backend for a hosted multi-tenant deployment
and fits behind the same interface; it costs a container, a password, a
backup story and a driver dependency for every install. (c) adds a
non-standard image and a second query language for queries that are six
recursive CTEs. If the 100k numbers in Phase 4 miss the budgets by more than
a small factor, (b) is the fallback, and the interface is the same.

**2. DynamoDB after the migration: supported map backend, or migration-only.**
Options: (a) a full `DynamoGraphStore` with history (a `valid_to` attribute,
one more GSI, `as_of` through filters); (b) current-graph only, history
methods raise, kept as the migration source and the rollback target; (c)
delete the DynamoDB map code after one release. *Recommendation: (b).*
Nothing in the open edition needs history on DynamoDB, and keeping the
current-graph store means rollback is a config flip. (a) is weeks of work for
a backend the open edition will not run; (c) removes the rollback.
Re-evaluate at 0.7 whether anyone still runs it.

**3. History retention.** Options: (a) 90 days and never fewer than the last
10 scans, pruned daily, as specified; (b) forever, prune by hand; (c) keep
only the last N scans. *Recommendation: (a).* It bounds disk without
configuration for the common case, and both knobs are in the config for the
rest. (b) surprises people with a growing file a year later; (c) makes "the
map last quarter" depend on scan frequency.

**4. Page sizes and caps.** Options for `nodes`: default 500 / max 5000
(specified), or default 200 / max 2000 (safer on slow laptops), or default
1000 / max 10000 (fewer round trips for the draw-whole case). `neighbors`
`max_nodes` default 500 / max 2000; `depth` max 6 (today's cap). `/api/maps`
compatibility endpoint keeps 2000/10000. *Recommendation: as specified*; the
draw-whole threshold of 3000 nodes fits in one page, and a page of 500 nodes
is ~450 KB, which is a fast request on localhost. Phase 5 numbers may move
them.

**5. Where the benchmark runs, and which budgets fail CI.** Options: (a) 10k
on every pull request that touches the map code, 100k nightly, 1M on demand,
budgets enforced at 10k and 100k; (b) nightly only, nothing on pull requests;
(c) everything on every pull request. *Recommendation: (a).* 10k on a PR is
about two minutes and catches an accidental whole-graph read or an N+1 in
review; 100k nightly catches the slow drift; 1M is a runner-sizing question
and a report. (b) finds regressions after merge; (c) makes every map PR wait
ten minutes.

**6. The fact store.** Options: (a) remove it: `source` and `scope` on the
graph rows carry the same information, and no reader exists; (b) keep it as a
write-only side table in the SQLite store for a host that reads facts; (c)
keep it on DynamoDB as today, untouched. *Recommendation: (a), as PR 8, after
the SQLite store ships.* It is a second write path that doubles the scan's
writes and is read by nothing; a host that needs claims-level provenance
gets it from the history versions, which record who said what when, per
scope. (c) keeps a DynamoDB write in a store that otherwise no longer uses
it; (b) builds a table for a reader that does not exist.

**7. The draw-whole threshold and the first-load shape.** 3000 nodes
(specified) versus 1000 (safer) versus "always summary first, draw nothing
until asked". *Recommendation: 3000, revisit in Phase 5 with the numbers*;
the seven-resource e2e world and a typical first install should look exactly
as they do today.
