#!/usr/bin/env bash
# Install root-owned at /usr/local/bin/mlop-deploy-dispatch.
# The deployment SSH key may only invoke this forced command.
set -Eeuo pipefail
umask 077

if [[ ! ${SSH_ORIGINAL_COMMAND:-} =~ ^deploy\ ([0-9a-f]{40})$ ]]; then
  echo 'Expected: deploy <full lowercase commit SHA>' >&2
  exit 2
fi
revision=${BASH_REMATCH[1]}
repo=/opt/mlop
state=/home/ubuntu/.local/state/mlop-deploy
mkdir -p "$state"
exec 9>"$state/deploy.lock"
flock -w 7200 9

cd "$repo"
source_status=$(git status --porcelain --untracked-files=all)
submodule_status=$(git submodule foreach --quiet --recursive 'git status --porcelain --untracked-files=all')
if [[ -n $source_status || -n $submodule_status ]]; then
  echo 'Deployment refused: reconcile local source changes first.' >&2
  exit 1
fi
git fetch --no-tags https://github.com/Theseus-Robotics/mlop.git main
main_revision=$(git rev-parse FETCH_HEAD)
if [[ $main_revision != "$revision" ]]; then
  echo 'Skipping superseded commit; only the current Theseus main may deploy.'
  exit 0
fi

script=$(mktemp "$state/deploy.XXXXXX")
trap 'rm -f "$script"' EXIT
git show "$revision:scripts/deploy-lightsail.sh" > "$script"
# Keep the dispatcher lock until the deployment, including recovery, finishes.
bash "$script" "$revision"
