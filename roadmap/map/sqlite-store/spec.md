---
title: "1.2 SQLite map store as the open default"
paths:
  - "infra_map/sql_store.py"
  - "infra_map/migrate.py"
criteria:
  - id: shipped
    text: "Node and edge tables indexed on both ends, WAL so the API and the daemon share one file, one transaction per scan."
    tests: [tests/infra_map/test_sqlite_store.py]
---

Node and edge tables indexed on both ends, WAL so the API and the daemon share one file, one transaction per scan. Recursive CTEs for traversals.

- Size: M, two to four weeks
- Shipped in 0.6.0 (PRs #12–#14)
- Design: [docs/specs/map-store.md](/docs/specs/map-store.md)
- Roadmap item 1.2
