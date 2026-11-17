---
title: "12.2 Software on the map"
paths:
  - "captains/mapper/software.py"
criteria:
  - id: shipped
    text: "The mapper records what each workload is made of, as purls: the image a task definition runs (resolved through ECR), Lambda runtime and layers, EC2 AMIs and SSM inventory, Dockerfile FROM and package lines, manifests and lockfiles per repo, and whether the workload faces the internet."
    tests: [tests/captains/mapper/test_software.py, tests/captains/mapper/test_software_collectors.py]
---

The mapper records what each workload is made of, as purls: the image a task definition runs (resolved through ECR), Lambda runtime and layers, EC2 AMIs and SSM inventory, Dockerfile FROM and package lines, manifests and lockfiles per repo, and whether the workload faces the internet.

- Size: M, two to four weeks
- Shipped in 0.9.0 (PR #25)
- Design: [docs/specs/patching-captain.md](/docs/specs/patching-captain.md)
- Roadmap item 12.2
