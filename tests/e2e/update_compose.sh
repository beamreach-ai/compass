#!/bin/sh
# An actual update of a compose stack, through the updater service (docs/open_updates_spec.md U6).
#
#   tests/e2e/update_compose.sh
#
# Builds Compass 0.4.0 from its tag and this tree as the version in pyproject.toml,
# pushes both to a registry on localhost, starts the shipped docker-compose.yml on
# 0.4.0 with the example captain installed and the updater on, and asks the updater:
#   1. for this build: it must come up healthy, migrate, rebuild the captain for the new core;
#   2. for a release whose digest is not the one listed: it must refuse and change nothing;
#   3. for a release that does not start: it must roll back to this build.
# Needs Docker with compose and network access to Docker Hub and PyPI (images, build
# dependencies); writes nothing outside a temporary directory and the local registry.
# The dashboard image is built once and tagged for every version: what is under test
# is the update of the stack, not the dashboard.
set -eu

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
PY="${PYTHON:-python3}"
VERSION=$("$PY" -c 'import tomllib;print(tomllib.load(open("'"$ROOT"'/pyproject.toml","rb"))["project"]["version"])')
FROM="${E2E_FROM:-0.4.0}"
BROKEN="${E2E_BROKEN:-9.9.9}"
PORT="${E2E_REGISTRY_PORT:-5055}"
REG="localhost:$PORT/e2e"
WORK="${E2E_DIR:-$(mktemp -d "${TMPDIR:-/tmp}/compass-e2e.XXXXXX")}"
PROJ="$WORK/compass-update-e2e"  # not the suite's project name: two stacks side by side
WEB="${E2E_PORT:-18080}"

log() { echo "e2e: $*"; }
fail() { echo "e2e: FAILED: $*" >&2; [ -f "$PROJ/extensions/updater.log" ] && tail -20 "$PROJ/extensions/updater.log" >&2; (cd "$PROJ" 2>/dev/null && docker compose --profile updater logs --tail 30 updater api >&2) || true; exit 1; }
dc() { (cd "$PROJ" && docker compose --profile updater "$@"); }
quiet() {  # what command...: run it, show its output only when it fails
  what=$1; shift
  "$@" > "$WORK/step.log" 2>&1 || { tail -40 "$WORK/step.log" >&2; fail "$what"; }
}
digest() { docker inspect --format '{{index .RepoDigests 0}}' "$1" | sed 's/.*@//'; }

cleanup() {
  [ "${E2E_KEEP:-0}" = "1" ] && { log "kept $WORK"; return; }
  (cd "$PROJ" 2>/dev/null && docker compose --profile updater down -v --remove-orphans >/dev/null 2>&1) || true
  docker rm -f compass-e2e-registry >/dev/null 2>&1 || true
  docker images --format '{{.Repository}}:{{.Tag}}' | grep "^$REG/" | xargs docker rmi -f >/dev/null 2>&1 || true
  git -C "$ROOT" worktree remove --force "$WORK/from" >/dev/null 2>&1 || true
  rm -rf "$ROOT/dist-e2e" "$WORK"
}
trap cleanup EXIT

log "updating $FROM -> $VERSION in $PROJ"
docker rm -f compass-e2e-registry >/dev/null 2>&1 || true  # one an interrupted run left behind
docker run -d --rm --name compass-e2e-registry -p "$PORT:5000" registry:2 >/dev/null

# ── the images ──────────────────────────────────────────────────────────────
"$PY" -m build --wheel --outdir "$ROOT/dist-e2e/new" "$ROOT" >/dev/null
docker build -q -f "$ROOT/docker/Dockerfile" --build-arg WHEEL=dist-e2e/new/*.whl -t "$REG/compass:$VERSION" "$ROOT" >/dev/null
docker build -q -f "$ROOT/docker/updater/Dockerfile" -t "$REG/compass-updater:$VERSION" "$ROOT" >/dev/null
docker build -q -f "$ROOT/dashboard/Dockerfile.dashboard" --build-arg NEXT_PUBLIC_EDITION=open --build-arg COMPASS_VERSION="$VERSION" \
  -t "$REG/compass-dashboard:$VERSION" "$ROOT" >/dev/null

git -C "$ROOT" worktree add --quiet --detach "$WORK/from" "v$FROM"
"$PY" -m build --wheel --outdir "$WORK/from/dist-e2e" "$WORK/from" >/dev/null
docker build -q -f "$WORK/from/docker/Dockerfile" --build-arg WHEEL=dist-e2e/*.whl -t "$REG/compass:$FROM" "$WORK/from" >/dev/null
# the stack starts with this build's updater: the old version had none
docker tag "$REG/compass-updater:$VERSION" "$REG/compass-updater:$FROM"
docker tag "$REG/compass-dashboard:$VERSION" "$REG/compass-dashboard:$FROM"

# a release that never becomes healthy
printf 'FROM busybox:1.36\nENTRYPOINT ["false"]\n' | docker build -q -t "$REG/compass:$BROKEN" - >/dev/null
docker tag "$REG/compass-dashboard:$VERSION" "$REG/compass-dashboard:$BROKEN"
docker tag "$REG/compass-updater:$VERSION" "$REG/compass-updater:$BROKEN"

for tag in "$FROM" "$VERSION" "$BROKEN"; do
  for image in compass compass-dashboard compass-updater; do docker push -q "$REG/$image:$tag" >/dev/null; done
done

# ── the stack on the old version ────────────────────────────────────────────
mkdir -p "$PROJ/extensions/wheels" "$PROJ/aws"
cp "$ROOT/docker-compose.yml" "$ROOT/nginx.conf" "$PROJ/"
# a profile with fake keys: the stack only talks to its own DynamoDB-local, never to AWS
printf '[default]\nregion = us-east-1\n' > "$PROJ/aws/config"
printf '[default]\naws_access_key_id = testing\naws_secret_access_key = testing\n' > "$PROJ/aws/credentials"
"$PY" -m build --wheel --outdir "$PROJ/extensions/wheels" "$ROOT/examples/captain-hello" >/dev/null
sed -e "s/^COMPASS_VERSION=.*/COMPASS_VERSION=$FROM/" -e "s/^COMPASS_PORT=.*/COMPASS_PORT=$WEB/" -e "s/^APP_PASSWORD=.*/APP_PASSWORD=e2e-password/" \
  "$ROOT/.env.example" > "$PROJ/.env"
cat >> "$PROJ/.env" <<ENV
COMPASS_REGISTRY=$REG
COMPASS_EXTENSIONS=compass-captain-hello==0.1.0
PIP_NO_INDEX=1
AWS_DIR=$PROJ/aws
COMPASS_UPDATE_CHECK=off
COMPASS_UPDATER_HEALTH_SECONDS=90
ENV

quiet init_config dc run --rm -T compass init_config workspace=local region=us-east-1 github_token=e2e
quiet init_tables dc run --rm -T compass init_tables
quiet "compose up on $FROM" dc up -d

wait_for() {  # seconds description command...
  seconds=$1; what=$2; shift 2
  deadline=$(( $(date +%s) + seconds ))
  until "$@" >/dev/null 2>&1; do
    [ "$(date +%s)" -lt "$deadline" ] || fail "$what within ${seconds}s"
    sleep 3
  done
}
running() { dc exec -T api python -c 'import compass_core;print(compass_core.__version__)' 2>/dev/null | tr -d '\r'; }
result() { jq -r "$1" "$PROJ/extensions/update.result.json" 2>/dev/null; }
request() {  # target digest-for-compass
  dashboard=$(digest "$REG/compass-dashboard:$1"); updater=$(digest "$REG/compass-updater:$1")
  rm -f "$PROJ/extensions/update.result.json"
  printf '{"target": "%s", "digests": {"compass": "%s", "compass-dashboard": "%s", "compass-updater": "%s"}, "requested_at": %s, "by": "e2e", "mode": "manual", "force": false, "wait_minutes": 5}\n' \
    "$1" "$2" "$dashboard" "$updater" "$(date +%s)" > "$PROJ/extensions/update.json"
}
settled() { s=$(result .status); [ "$s" = updated ] || [ "$s" = failed ]; }

wait_for 240 "$FROM to answer /health" curl -fsS "http://localhost:$WEB/health"
wait_for 60 "the updater's heartbeat" test -f "$PROJ/extensions/updater.json"
[ "$(running)" = "$FROM" ] || fail "the stack runs $(running), not $FROM"
log "$FROM is up with the updater"

# 1. this build
request "$VERSION" "$(digest "$REG/compass:$VERSION")"
wait_for 600 "the update to $VERSION to settle" settled
[ "$(result .status)" = updated ] || fail "update to $VERSION: $(result .error)"
[ "$(running)" = "$VERSION" ] || fail "after the update the API runs $(running)"
grep -q "^COMPASS_VERSION=$VERSION\$" "$PROJ/.env" || fail ".env does not pin $VERSION"
grep -q "^COMPASS_VERSION=$FROM\$" "$PROJ/.env.bak.$FROM" || fail "no .env.bak.$FROM"
[ "$(jq -r .to "$PROJ/extensions/rollback.json")" = "$FROM" ] || fail "no rollback offer to $FROM"
wait_for 120 "the captain rebuilt for $VERSION" sh -c "jq -e '.core == \"$VERSION\"' '$PROJ/extensions/installed.json'"
dc exec -T -e PYTHONPATH=/compass/extensions/site api compass captains 2>/dev/null | jq -e '.captains[] | select(.name == "hello" and .compatible)' >/dev/null || fail "the hello captain is not loaded on $VERSION"
dc exec -T api compass update check 2>/dev/null | jq -e ".rollback.to == \"$FROM\" and .result.status == \"updated\"" >/dev/null || fail "compass update check does not show the update"
log "updated $FROM -> $VERSION, captain rebuilt, rollback offered"

# 2. an image that is not the one listed
request "$BROKEN" "sha256:0000000000000000000000000000000000000000000000000000000000000000"
wait_for 180 "the digest refusal" settled
[ "$(result .status)" = failed ] && result .error | grep -q "not the image the release lists" || fail "a wrong digest was not refused: $(result .error)"
grep -q "^COMPASS_VERSION=$VERSION\$" "$PROJ/.env" || fail ".env changed on a refused update"
log "a wrong digest is refused"

# 3. a release that does not start
request "$BROKEN" "$(digest "$REG/compass:$BROKEN")"
wait_for 600 "the broken update to settle" settled
[ "$(result .status)" = failed ] && result .error | grep -q "rolled back to $VERSION" || fail "a broken release was not rolled back: $(result .error)"
wait_for 240 "$VERSION to answer /health again" curl -fsS "http://localhost:$WEB/health"
[ "$(running)" = "$VERSION" ] || fail "after the rollback the API runs $(running)"
grep -q "^COMPASS_VERSION=$VERSION\$" "$PROJ/.env" || fail ".env was not restored"
log "a broken release rolls back to $VERSION"
log "passed"
