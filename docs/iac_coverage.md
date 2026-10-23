# IaC coverage: what is not in Terraform, and getting it in

Two questions come up in every AWS account that has been around for a while:
which resources were created by hand and are in no Terraform at all, and
which Terraform-managed resources no longer match their state. Compass answers
both from the map, and turns the answer into pull requests.

## How the map decides

A map scan (Maps → Run mapping, or `compass mapper run`) reads three things:

1. **The AWS account**, through the read role `compass connect` created, in
   the regions listed in the workspace config (`aws_regions`; `init_config`
   sets the one region you gave it). It reads EC2 (instances, security groups, network interfaces), ECS,
   ECR, Lambda, S3, IAM roles, RDS, DynamoDB, SQS, SNS, Secrets Manager, load
   balancers, CloudWatch logs and alarms, EventBridge rules and Step
   Functions. Other services are not in the map.
2. **Your GitHub repositories**: the Terraform root modules in them and their
   `backend "s3"` blocks, up to `github.max_repos` repositories (25 by
   default).
3. **The Terraform state** those backends point to, read from S3. The read
   role may read state only from the bucket you name with
   `compass connect … state_bucket=<bucket>`: state files hold secrets in
   plain text, so that permission stays scoped.

Each resource in a state file is matched to the live resource it describes.
A live resource that some state describes is *managed*; one that none does is
*unmanaged*. For managed resources the scan compares a few attributes per
type between state and AWS (a Lambda function's memory, timeout and runtime;
an instance's type; an ECS service's desired count; a log group's retention;
and others) and marks the resource *in sync* or *drifted*, with the fields that
differ.

State that is not in S3, or repositories Compass cannot see with your GitHub
token, leave their resources looking unmanaged.

## Seeing it

- The **IAC** tab: coverage per resource type, the unmanaged list and the
  drifted list.
- The CLI in `cli/`: `compass iac coverage`, `compass iac unmanaged`,
  `compass iac drifted`.
- `GET /api/iac/coverage` returns the same as JSON.
- `compass mapper export format=json` writes the whole map; each AWS node
  carries `coverage_score` (1 managed, 0 unmanaged) and `drift_status`.

## Importing unmanaged resources

```sh
docker compose run --rm compass iac import repo=<owner>/<infra-repo> module_path=<root> \
    resource_ids='["arn:aws:s3:::my-bucket"]'
```

or select resources on the IAC tab. The job:

1. Clones the repository (a real checkout, so nothing you wrote is
   overwritten) and writes `import` blocks for the resources into the root
   module you named.
2. Runs `terraform plan -generate-config-out` with read-role credentials, so
   Terraform itself writes the configuration from the live resources.
3. Cleans it: drops computed attributes such as `tags_all`, null filler and
   values the provider would reject.
4. Removes secrets: attributes the provider schema marks sensitive are taken
   out, not masked. If the provider schema cannot be read, the job fails
   rather than guess which values are secret.
5. Rewires literals to references: `vpc_id = "vpc-0abc…"` becomes the module
   output or resource attribute in the same root module that evaluates to that
   value. This step is deterministic; no model is involved.
6. Verifies with `terraform plan` (read-only, `-lock=false`). If the plan is
   not clean, the pull request still opens, labelled, with the plan in its
   body.
7. Opens the pull request on a `beamreach/iac-import/<hash>` branch. Its body
   says what was verified, what was rewired and what was skipped or redacted.

You review it and run `terraform apply`, which completes the import. Compass
never applies. The Compass image includes Terraform; without Terraform the job
falls back to configuration written by the model, unverified, and says so.

Resource types it can import: Lambda functions, EC2 instances and security
groups, ECS clusters, services and task definitions, load balancers and
target groups, DynamoDB tables, SQS queues, SNS topics, Secrets Manager
secrets, IAM roles, CloudWatch log groups and metric alarms, EventBridge
rules, Step Functions state machines, S3 buckets, RDS instances and clusters.
Anything else is listed as skipped with the reason, not guessed at.

## Fixing drift

`compass iac fix_drift resource_ids='[...]'` (or Fix drift on the IAC tab)
opens a pull request that brings the Terraform back in line with what runs.
It needs a model (`ANTHROPIC_API_KEY`), which writes the HCL change; without
one the job fails with that reason.

## Pull requests after they open

The PR watcher follows every pull request Compass opened: it answers review
comments and, with a model, pushes the revision a reviewer asks for when the
watcher's policy allows (`prs.watcher.mode`: `off`, `approve` or `autopilot`
within `risk_max`). It never merges.
