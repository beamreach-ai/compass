#!/bin/sh
# The Compass updater (docs/open_updates_spec.md §4.1): applies the update the
# dashboard, `compass update apply` or the policy asked for, and nothing else.
#
# Every 30 seconds it reads /compass/extensions/update.json. When there is a
# request: preflight (a version, an .env, a compose file that parses, no job
# running unless the request says force, and for an automatic request a policy
# that still allows it), pull the release's images and refuse one whose digest
# is not the one the release lists, back up .env and set COMPASS_VERSION, then
# recreate the services and wait for the API and the dashboard to answer. When
# they do not within five minutes, .env goes back and the previous version is
# recreated. Every step is written to update.result.json for the page.
#
# It recreates api, compass, dashboard and nginx and never itself: a compose
# process cannot survive the recreation of its own container. It keeps running
# its current image until the next `docker compose --profile updater up -d`.
#
# The project directory is mounted at its own host path and compose runs with
# that as its project directory, so the bind mounts compose resolves are the
# host's paths. .env is rewritten in place (truncate and write), so the file
# keeps its owner and mode.
set -u

PROJECT="${COMPASS_PROJECT:?COMPASS_PROJECT is the compose project directory}"
EXT="${COMPASS_EXTENSIONS_DIR:-/compass/extensions}"
REGISTRY="${COMPASS_REGISTRY:-ghcr.io/beamreach-ai}"
POLL="${COMPASS_UPDATER_POLL:-30}"
HEALTH_SECONDS="${COMPASS_UPDATER_HEALTH_SECONDS:-300}"
SELF_VERSION="${COMPASS_VERSION:-unknown}"
SERVICES="api compass dashboard nginx"
IMAGES="compass compass-dashboard compass-updater"
export COMPOSE_PROFILES="${COMPOSE_PROFILES:-updater}"

# The compose project this container belongs to (the host named it after its directory, or
# COMPOSE_PROJECT_NAME): read from this container's own label, so the updater never starts a
# second stack beside the one it is meant to update.
PROJECT_NAME="${COMPASS_COMPOSE_PROJECT:-$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$(hostname)" 2>/dev/null)}"
[ -n "$PROJECT_NAME" ] || PROJECT_NAME=$(basename "$PROJECT")

# Compose prefers the process environment over .env: run it without the variables this
# container was started with, so the .env just written is what decides the version.
compose() { env -u COMPASS_VERSION -u COMPASS_REGISTRY docker compose --project-directory "$PROJECT" -p "$PROJECT_NAME" "$@"; }
log() { echo "updater: $*"; }
now() { date +%s; }
write_file() { printf '%s\n' "$2" > "$1.tmp" && mv "$1.tmp" "$1"; }

# status from to mode error
result() {
  write_file "$EXT/update.result.json" "$(jq -n --arg s "$1" --arg f "$2" --arg t "$3" --arg m "$4" --arg e "$5" --argjson at "$(now)" \
    '{status: $s, from: $f, to: $t, mode: $m, error: (if $e == "" then null else $e end), at: $at}')"
}

heartbeat() { write_file "$EXT/updater.json" "{\"at\": $(now), \"version\": \"$SELF_VERSION\"}"; }

env_version() { sed -n 's/^COMPASS_VERSION=//p' "$PROJECT/.env" | tail -1; }

# version: rewrite the COMPASS_VERSION line (or append one), keeping the file's owner and mode
set_env_version() {
  if grep -q '^COMPASS_VERSION=' "$PROJECT/.env"; then
    text=$(sed "s/^COMPASS_VERSION=.*/COMPASS_VERSION=$1/" "$PROJECT/.env")
  else
    text=$(cat "$PROJECT/.env"; printf '\nCOMPASS_VERSION=%s' "$1")
  fi
  printf '%s\n' "$text" > "$PROJECT/.env"
}

restore_env() {  # version
  if [ -f "$PROJECT/.env.bak.$1" ]; then cat "$PROJECT/.env.bak.$1" > "$PROJECT/.env"; else set_env_version "$1"; fi
}

# version: the API and the daemon run that version's image, and the API and the dashboard answer
# compose up, once more after a pause if it fails: a transient error from Compose or the
# daemon is not a failed release, and rolling back for one would be wrong
compose_up() {
  compose up -d --remove-orphans $SERVICES >>"$EXT/updater.log" 2>&1 && return 0
  log "docker compose up failed; trying once more"
  sleep 5
  compose up -d --remove-orphans $SERVICES >>"$EXT/updater.log" 2>&1
}

healthy() {
  for service in api compass; do
    id=$(compose ps -q "$service" 2>/dev/null | head -1)
    [ -n "$id" ] || return 1
    case "$(docker inspect --format '{{.Config.Image}}' "$id" 2>/dev/null)" in *":$1") ;; *) return 1 ;; esac
  done
  curl -fsS -m 5 -o /dev/null http://api:8001/health 2>/dev/null && curl -fsS -m 5 -o /dev/null http://dashboard:3000/ 2>/dev/null
}

# refuse: from to mode why
refuse() {
  log "refused: $4"
  result failed "$1" "$2" "$3" "$4"
  rm -f "$EXT/update.json"
}

# image digest: pull unless the local image already carries the digest; verify when one is given
pull_verified() {
  if [ -n "$2" ] && docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$1" 2>/dev/null | grep -q "@$2\$"; then
    return 0
  fi
  docker pull -q "$1" >/dev/null 2>"$EXT/updater.err" || { echo "pull of $1 failed: $(tail -1 "$EXT/updater.err")"; return 1; }
  if [ -n "$2" ] && ! docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$1" | grep -q "@$2\$"; then
    echo "$1 is not the image the release lists (digest $2); not applied"
    return 1
  fi
}

# the part after `compose up`: wait for health, then record the result or roll back
finish_pending() {
  pend="$EXT/update.pending"
  [ -f "$pend" ] || return 0
  from=$(jq -r '.from' "$pend"); to=$(jq -r '.to' "$pend"); mode=$(jq -r '.mode' "$pend"); rollback=$(jq -r '.rollback // false' "$pend")
  log "waiting for $to to come up"
  deadline=$(( $(now) + HEALTH_SECONDS ))
  while [ "$(now)" -lt "$deadline" ]; do
    if healthy "$to"; then
      rm -f "$pend"
      result updated "$from" "$to" "$mode" ""
      if [ "$rollback" = "true" ]; then
        rm -f "$EXT/rollback.json"
      else
        write_file "$EXT/rollback.json" "{\"to\": \"$from\", \"from\": \"$to\", \"until\": $(( $(now) + 7 * 86400 ))}"
      fi
      log "updated $from -> $to"
      return 0
    fi
    sleep 5
  done
  log "$to did not become healthy in ${HEALTH_SECONDS}s; restoring $from"
  rm -f "$pend"
  restore_env "$from"
  result failed "$from" "$to" "$mode" "not healthy within ${HEALTH_SECONDS}s of the update (the API's /health and the dashboard); rolled back to $from"
  compose_up || log "rollback compose up failed; see $EXT/updater.log"
}

handle_request() {
  req="$EXT/update.json"
  [ -f "$req" ] || return 0
  target=$(jq -r '.target // empty' "$req" 2>/dev/null)
  mode=$(jq -r '.mode // "manual"' "$req" 2>/dev/null)
  force=$(jq -r '.force // false' "$req" 2>/dev/null)
  rollback=$(jq -r '.rollback // false' "$req" 2>/dev/null)
  wait_minutes=$(jq -r '.wait_minutes // 60' "$req" 2>/dev/null)
  requested_at=$(jq -r '.requested_at // 0' "$req" 2>/dev/null | cut -d. -f1)
  from=$(env_version)
  [ -n "$from" ] || from="$SELF_VERSION"

  if ! echo "$target" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+([a-z]+[0-9]+)?$'; then refuse "$from" "$target" "$mode" "target '$target' is not a version"; return; fi
  if [ ! -f "$PROJECT/.env" ]; then refuse "$from" "$target" "$mode" "no .env in $PROJECT"; return; fi
  if ! compose config -q 2>"$EXT/updater.err"; then refuse "$from" "$target" "$mode" "docker compose config: $(tail -1 "$EXT/updater.err")"; return; fi
  if [ "$mode" = "auto" ]; then
    auto=$(jq -r '.auto // "notify"' "$EXT/update.policy.json" 2>/dev/null || echo notify)
    case "$auto" in patch|minor) ;; *) refuse "$from" "$target" "$mode" "automatic updates are '$auto' now"; return ;; esac
  fi
  active=$(jq -r '.active_jobs // 0' "$EXT/status.json" 2>/dev/null || echo 0)
  if [ "${active:-0}" -gt 0 ] && [ "$force" != "true" ]; then
    if [ $(( $(now) - requested_at )) -gt $(( wait_minutes * 60 )) ]; then
      refuse "$from" "$target" "$mode" "$active jobs were still running after $wait_minutes minutes"
    else
      result waiting "$from" "$target" "$mode" "$active jobs are running; the update starts when they finish"
    fi
    return
  fi

  log "updating $from -> $target ($mode)"
  result pulling "$from" "$target" "$mode" ""
  for key in $IMAGES; do
    want=$(jq -r --arg k "$key" '.digests[$k] // empty' "$req")
    if [ -z "$want" ] && [ "$rollback" = "true" ] && docker image inspect "$REGISTRY/$key:$target" >/dev/null 2>&1; then
      continue  # the previous version's image is still here; nothing to pull
    fi
    if ! error=$(pull_verified "$REGISTRY/$key:$target" "$want"); then refuse "$from" "$target" "$mode" "$error"; return; fi
  done

  cat "$PROJECT/.env" > "$PROJECT/.env.bak.$from"
  set_env_version "$target"
  write_file "$EXT/update.pending" "{\"from\": \"$from\", \"to\": \"$target\", \"mode\": \"$mode\", \"rollback\": $rollback, \"started_at\": $(now)}"
  rm -f "$req"
  result applying "$from" "$target" "$mode" ""
  : > "$EXT/updater.log"
  if ! compose_up; then
    log "docker compose up failed; restoring $from"
    rm -f "$EXT/update.pending"
    restore_env "$from"
    result failed "$from" "$target" "$mode" "docker compose up: $(tail -1 "$EXT/updater.log")"
    compose_up || log "rollback compose up failed; see $EXT/updater.log"
    return
  fi
  finish_pending
}

log "compass updater $SELF_VERSION watching $EXT/update.json for project $PROJECT_NAME in $PROJECT"
mkdir -p "$EXT"
finish_pending   # an update this container did not see through (it was restarted in the middle)
while true; do
  heartbeat
  handle_request || log "request handling failed: $?"
  sleep "$POLL"
done
