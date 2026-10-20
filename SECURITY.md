# Security

Compass runs with read access to an AWS account, writes pull requests to a
repository, and — when you enable decoys — plants IAM users and EventBridge
rules whose only purpose is to be used by an attacker. Bugs here can matter.

## Reporting a vulnerability

Email **ilia+security@beamreach.ai**. Say what you found, how to reproduce it, and
which version (`compass --version`, or the image tag in your `.env`). You will
get an acknowledgement within three working days and a fix or a plan within
thirty. Please do not open a public issue for a vulnerability until a fix is
out.

## What is in scope

- The `compass` daemon and API, the dashboard, and the images in
  `docker-compose.yml`.
- The Terraform Compass generates (decoy modules, import blocks, drift fixes)
  and the pull requests it opens.
- The decoys themselves: a decoy credential that could be used for anything
  other than tripping the alarm is a vulnerability.

Out of scope: the Beamreach hosted product and website; report those to the
same address but they are handled separately.

## How Compass handles credentials

- Your AWS profile is used by one command, `compass connect`, which creates two
  IAM roles, an alert queue and a runtime user, once; the running services
  never see it and refuse to start with one in reach. The runtime user's key is
  the one AWS credential Compass stores (`config/local.json`); its only right
  is to assume the two roles. Compass reads the account through the read role,
  narrowed per captain to what its manifest declares, and writes nothing
  without a pull request you merge (docs/access.md).
- The GitHub token and the dashboard password live in `config/local.json` and
  `.env` in the directory you installed into — mode 600, never in a repository,
  never sent anywhere but github.com and your own browser.
- The decoy credentials Compass plants are generated in your account by
  Terraform; the secret stays in your Terraform state and outputs. Compass
  stores the key id, never the secret.
- Decoy alerts arrive through an SQS queue in your account; nothing is sent to
  Beamreach.
