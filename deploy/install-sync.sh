#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID != 0 ]]; then
  echo 'Execute como root.' >&2
  exit 1
fi
for cmd in git systemctl install runuser useradd; do
  command -v "$cmd" >/dev/null
done

account=facilpost-sync
base=/var/www/facilpost
if ! id "$account" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$base" --no-create-home --shell /usr/sbin/nologin "$account"
fi
install -d -m 0755 "$base"

for component in backend frontend; do
  repo="https://github.com/ViniciusdAvilla/FacilPost-${component^}.git"
  dest="$base/$component"
  if [[ ! -e "$dest" ]]; then
    staging=$(mktemp -d "$base/.clone-XXXXXX")
    trap 'rm -rf -- "$staging"' EXIT
    chown "$account" "$staging"
    runuser -u "$account" -- git clone --single-branch --branch main "$repo" "$staging"
    chmod 0755 "$staging"
    mv -T "$staging" "$dest"
    trap - EXIT
  fi
  actual=$(runuser -u "$account" -- git -C "$dest" remote get-url origin)
  [[ "$actual" == "$repo" ]] || { echo "Origem inesperada em $dest" >&2; exit 1; }
done

cat >/etc/systemd/system/facilpost-sync@.service <<'UNIT'
[Unit]
Description=Sincronizar FácilPost %i com GitHub
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=facilpost-sync
WorkingDirectory=/var/www/facilpost/%i
Environment=GIT_TERMINAL_PROMPT=0
ExecStartPre=/bin/sh -ec 'test "$(git symbolic-ref --short HEAD)" = main && test -z "$(git status --porcelain)"'
ExecStart=/usr/bin/git -c core.hooksPath=/dev/null pull --ff-only origin main
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/www/facilpost/%i
PrivateTmp=yes
UNIT

cat >/etc/systemd/system/facilpost-sync@.timer <<'UNIT'
[Unit]
Description=Verificar atualizações do FácilPost %i

[Timer]
OnBootSec=30s
OnUnitInactiveSec=60s
AccuracySec=5s

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
for component in backend frontend; do
  systemctl start "facilpost-sync@$component.service"
  systemctl enable --now "facilpost-sync@$component.timer"
done
