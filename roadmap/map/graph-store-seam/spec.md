---
title: "1.1 GraphStore seam"
paths:
  - "infra_map/graph_store.py"
  - "infra_map/dynamo_store.py"
  - "infra_map/store.py"
criteria:
  - id: shipped
    text: "One interface for upserts, stale deletes, neighbours, paths and queries."
    tests: [tests/infra_map/test_graph_store_contract.py]
---

One interface for upserts, stale deletes, neighbours, paths and queries. DynamoDB stays as one implementation, registered like queue and config.

- Size: M, two to four weeks
- Shipped in 0.6.0 (PRs #12–#14)
- Design: [docs/specs/map-store.md](/docs/specs/map-store.md)
- Roadmap item 1.1
