# The map benchmark

What the map costs at 10k, 100k and 1M resources, on each store, against the
budgets in [docs/specs/map-store.md](specs/map-store.md) section 10. The code
is `tests/benchmark/`: `world.py` generates the account, `harness.py` runs the
measurements, `adapters.py` is each store seen through `GraphStore`.

## Run it

```sh
# a throwaway DynamoDB-local and Postgres, on ports nothing else uses
docker run -d --name compass-bench-dynamodb -p 18000:8000 amazon/dynamodb-local -jar DynamoDBLocal.jar -inMemory -sharedDb
docker run -d --name compass-bench-postgres -p 15432:5432 -e POSTGRES_PASSWORD=bench -e POSTGRES_USER=bench -e POSTGRES_DB=bench postgres:16-alpine
pip install -e ".[dev,postgres]"

.venv/bin/python -m tests.benchmark.run --scale 10k --store legacy,dynamodb,sqlite,postgres --out bench-10k.json
```

| Store | What it measures |
|---|---|
| `legacy` | the code before the `GraphStore` interface: `InfraMapStore` and the mapper's write block, on DynamoDB |
| `dynamodb` | the same table through `DynamoGraphStore` |
| `sqlite` | `SqliteGraphStore` on a temp file (`BENCH_SQLITE_PATH` to choose) |
| `postgres` | `PostgresGraphStore` (`BENCH_POSTGRES_DSN`, default the container above) |

`BENCH_DYNAMODB_URL` points the two DynamoDB stores elsewhere. `--scale 100k`
takes minutes on the SQL stores and the better part of an hour on DynamoDB;
`--scale 1m` is for a machine with time. Run one store at a time when the
numbers matter: a DynamoDB-local JVM next to Postgres skews both.

`pytest -m benchmark` runs the 10k point on SQLite with the budgets enforced;
the default `pytest -q` skips it (`-m "not benchmark"` in `pyproject.toml`).

## The world

Deterministic from a seed. Per thousand resources: 150 EC2 instances, 150
network interfaces, 130 log groups, 120 Lambda functions, 100 security groups,
60 roles, 40 buckets, and the rest down to 3 ECS clusters, in four regions split
50/25/15/10. A few roles and security groups carry most of the edges (the
busiest role at 100k has 16,589 incoming edges), which is what a neighbourhood
query has to survive. Ownership nodes cover 60% of the resources: a repository
per 2,500 resources, a root module per 200, a state file per root module, a
state instance per managed resource, wired with `observes`, `contains`,
`writes` and `part_of` so the resolver walks what it walks on a real map; 3% of
the managed resources are drifted. At 10k that is 16,104 nodes and 26,166
edges; at 100k, 161,040 and 261,461.

The second scan (`churn`) removes 5% of the resources, adds as many new ones
wired to what is there, and changes a tag on 2% of the rest.

## What is measured

| Metric | What |
|---|---|
| `scan_first_s`, `scan_second_s` | the mapper's write after collection: the first world, then the churned one |
| `fetch_graph_s` | the whole-graph read (what the migration and today's whole-graph callers pay) |
| `first_load_s`, `first_load_bytes` | the dashboard's first request: `GET /api/maps?limit=4000` on the legacy path; the summary, plus every page under the draw-whole threshold, on the others |
| `neighborhood_hub_ms`, `neighborhood_ms` | depth 2 around the busiest role and around a median instance, capped at 500 nodes |
| `page_ms` | 500 Lambda functions (a whole-graph load and a filter on the legacy path) |
| `summary_ms` | counts by type (the IaC coverage card) |
| `ownership_ms` | the ownership resolver for one managed resource, as it is today |
| `paths_ms` | shortest paths from a function to a bucket, depth 4 (no equivalent on the legacy path) |
| `peak_rss_mb`, `db_size_mb` | the process, and the store on disk |

Results so far are in the spec, section 10.5.
