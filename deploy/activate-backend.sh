#!/usr/bin/env bash
# Installed as a root-owned helper. Repository code and dependency installation run unprivileged.
set -euo pipefail
exec 9>/run/facilpost-activate.lock
flock 9
repo=/var/www/facilpost/backend
base=/opt/facilpost
revision=$(runuser -u facilpost-sync -- git -C "$repo" rev-parse --verify 'HEAD^{commit}')
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
release="$base/releases/$revision"
previous=$(readlink "$base/current" || true)

if [[ "$previous" == "$release" ]] && systemctl is-active --quiet facilpost-api; then
  exit 0
fi

if [[ "$previous" != "$release" ]]; then
  # An interrupted build may have left a partial environment. Rebuild from the commit.
  rm -rf -- "$release"
  install -d -m 0755 -o facilpost-build -g facilpost-build "$release"
  runuser -u facilpost-sync -- git -C "$repo" archive "$revision" |
    runuser -u facilpost-build -- tar -x --no-same-owner -C "$release"
  runuser -u facilpost-build -- python3 -m venv "$release/.venv"
  runuser -u facilpost-build -- "$release/.venv/bin/pip" --disable-pip-version-check install --no-cache-dir -r "$release/requirements.txt"
fi

link_current() {
  ln -sfnT "$1" "$base/current.next"
  mv -Tf "$base/current.next" "$base/current"
}

link_current "$release"
healthy=false
if systemctl restart facilpost-api; then
  for _ in {1..20}; do
    if curl --silent --fail --max-time 2 http://127.0.0.1:8187/api/health >/dev/null; then
      healthy=true
      break
    fi
    sleep 1
  done
fi

if [[ "$healthy" == false ]]; then
  if [[ -n "$previous" && "$previous" != "$release" ]]; then
    link_current "$previous"
    systemctl restart facilpost-api
  else
    systemctl stop facilpost-api
    rm -f "$base/current"
  fi
  echo 'A nova API falhou na verificação de saúde. A próxima execução tentará novamente.' >&2
  exit 1
fi

# Keep one fallback release and the running release.
for dir in "$base"/releases/*; do
  if [[ "$dir" != "$release" && "$dir" != "$previous" && -d "$dir" ]]; then
    rm -rf -- "$dir"
  fi
done
