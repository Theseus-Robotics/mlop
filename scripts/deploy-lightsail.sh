#!/usr/bin/env bash
# Called by lightsail-dispatch.sh after locking and verifying the main SHA.
set -Eeuo pipefail
umask 077

revision=${1:?Expected commit SHA}
[[ $revision =~ ^[0-9a-f]{40}$ ]] || exit 2
repo=/opt/mlop
state=/home/ubuntu/.local/state/mlop-deploy
services=(backend frontend ingest py)
cd "$repo"
previous=$(git rev-parse HEAD)
backup="$state/releases/$(date -u +%Y%m%dT%H%M%SZ)-${revision:0:12}"
mkdir -p "$backup"
chmod 600 server/.env
printf '%s\n' "$previous" > "$backup/previous-sha"
cp server/docker-compose.yml "$backup/compose.yml"
compose=(sudo -n docker compose --project-name server --project-directory "$repo/server" --env-file "$repo/server/.env" -f "$repo/server/docker-compose.yml")
source_changed=0
replacement_started=0

probe() {
  local url=$1
  local attempt
  for ((attempt = 0; attempt < 60; attempt++)); do
    if curl --fail --silent --output /dev/null --connect-timeout 3 --max-time 5 "$url"; then
      return 0
    fi
    sleep 2
  done
  printf 'Health check failed: %s\n' "$url" >&2
  return 1
}

check_health() {
  probe http://127.0.0.1:3000/ || return 1
  probe http://127.0.0.1:3001/api/health || return 1
  probe http://127.0.0.1:3003/health || return 1
  probe http://127.0.0.1:3004/openapi.json || return 1
  probe https://ml-ops.dev.theseusrobotics.ch/ || return 1
  probe https://api.ml-ops.dev.theseusrobotics.ch/api/health || return 1
  probe https://ingest.ml-ops.dev.theseusrobotics.ch/health || return 1
  probe https://py.ml-ops.dev.theseusrobotics.ch/openapi.json || return 1
}

recover() {
  local result=$?
  trap - EXIT HUP INT TERM
  if (( result != 0 && source_changed )); then
    echo 'Deployment failed; restoring previous source.' >&2
    # Fail closed if source restoration fails; do not use a mixed Compose file.
    if git checkout --detach "$previous" &&
       git submodule sync --recursive &&
       git submodule update --init --recursive; then
      if (( replacement_started )); then
        if "${compose[@]}" -f "$backup/images.yml" up -d --no-deps --no-build --pull never "${services[@]}" && check_health; then
          echo 'Previous application images restored.' >&2
        else
          echo 'Automatic application recovery failed; operator intervention required.' >&2
        fi
        echo 'Database schema is not automatically reverted.' >&2
      fi
    else
      echo 'Source recovery failed; operator intervention required.' >&2
    fi
    printf 'Recovery files: %s\n' "$backup" >&2
  fi
  exit "$result"
}
trap recover EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

# Stop before changes if backups/builds would have too little room.
available_kb=$(df -Pk "$repo" | awk 'END {print $4}')
if (( available_kb < 8 * 1024 * 1024 )); then
  echo 'Deployment requires at least 8 GiB free; inspect disk usage first.' >&2
  exit 1
fi

printf 'services:\n' > "$backup/images.yml"
for service in "${services[@]}"; do
  container=$("${compose[@]}" ps -q "$service")
  [[ -n $container ]] || { echo "Missing running service: $service" >&2; exit 1; }
  image=$(sudo -n docker inspect --format '{{.Image}}' "$container")
  tag="mlop-rollback/$service:$(basename "$backup")"
  sudo -n docker image tag "$image" "$tag"
  printf '  %s:\n    image: %s\n' "$service" "$tag" >> "$backup/images.yml"
done

# Password remains inside the database container; backup never leaves this host.
database=$("${compose[@]}" ps -q db)
sudo -n docker exec "$database" sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" exec pg_dumpall --no-password -U "$POSTGRES_USER" -l postgres' |
  gzip > "$backup/postgres.sql.gz"
gzip -t "$backup/postgres.sql.gz"

source_changed=1
git checkout --detach "$revision"
git submodule sync --recursive
git submodule update --init --recursive

# Only server/.env is allowed. Child build contexts must contain no real env files.
unexpected_env=$(find server/web server/ingest server/py \( -type f -o -type l \) -name '.env*' ! -name '*.example' -print -quit)
if [[ -n $unexpected_env ]]; then
  echo 'Deployment refused: a build context contains a non-example .env file.' >&2
  exit 1
fi
"${compose[@]}" config --quiet

# Serial builds fit this Lightsail instance and preserve existing base-image caches.
for service in "${services[@]}"; do
  "${compose[@]}" build "$service"
done

replacement_started=1
"${compose[@]}" up -d --no-deps --no-build --pull never "${services[@]}"
check_health

printf '%s\n' "$revision" > "$state/deployed-sha.tmp"
mv "$state/deployed-sha.tmp" "$state/deployed-sha"
printf 'Deployed %s\n' "$revision"

# Cleanup is independent of deployment success. Retain three recovery snapshots.
source_changed=0
if ! python3 - "$state/releases" "$backup" <<'PY'
import pathlib
import re
import shutil
import subprocess
import sys

root = pathlib.Path(sys.argv[1])
current = pathlib.Path(sys.argv[2])
releases = sorted(
    path for path in root.iterdir()
    if path.is_dir() and not path.is_symlink()
    and re.fullmatch(r"\d{8}T\d{6}Z-[0-9a-f]{12}", path.name)
)
for release in releases[:-3]:
    if release == current:
        continue
    for service in ("backend", "frontend", "ingest", "py"):
        tag = f"mlop-rollback/{service}:{release.name}"
        exists = subprocess.check_output(
            ["sudo", "-n", "docker", "image", "ls", "--quiet", "--filter", f"reference={tag}"],
            text=True,
        ).strip()
        if exists:
            subprocess.run(
                ["sudo", "-n", "docker", "image", "rm", tag],
                check=True, stdout=subprocess.DEVNULL,
            )
    shutil.rmtree(release)
PY
then
  echo 'Deployment succeeded; obsolete recovery files need manual cleanup.' >&2
fi
