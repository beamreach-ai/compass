"""What the open-source packages share (docs/open_source_split_spec.md §3).

keys          DynamoDB key builders for every record type
tables        which config key names which table
registry      the pull-request registry
policy        deterministic risk/urgency scoring and the mode/risk gate
aws           target-account sessions, regions, credentials
secrets       JSON secrets from the environment or Secrets Manager
github        GitHub App tokens and installation lookups
github_prs    publishing a pull request through the Contents API
git_metadata  remote/branch probing and slug sanitising

The old import paths under workspace/, utils/ and nodes/triage/ re-export
these until the private side has moved over.
"""
