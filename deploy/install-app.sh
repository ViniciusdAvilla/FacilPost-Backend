#!/usr/bin/env bash
set -euo pipefail

host=facilpost.myfluxhub.tech
base=/var/www/facilpost
sources=$(cd -- "$(dirname -- "$0")" && pwd)
[[ $EUID == 0 ]] || { echo 'Execute com sudo.' >&2; exit 1; }
for command in nginx certbot git curl systemctl python3 runuser flock ss; do
  command -v "$command" >/dev/null || { echo "Comando necessário: $command" >&2; exit 1; }
done
[[ -f "$base/backend/app.py" && -f "$base/frontend/public/index.html" ]] ||
  { echo 'Execute deploy/install-sync.sh e confirme os dois repositórios em /var/www/facilpost.' >&2; exit 1; }

exec 8>/run/facilpost-install.lock
flock 8
if ss -Hln 'sport = :8187' | awk 'NF {used=1} END {exit !used}' && ! systemctl is-active --quiet facilpost-api; then
  echo 'A porta local 8187 está em uso por outro processo.' >&2
  exit 1
fi

for account in facilpost-api facilpost-build; do
  if ! id "$account" >/dev/null 2>&1; then
    useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin "$account"
  fi
done
install -d -m 0755 /opt/facilpost/releases /usr/local/libexec /etc/facilpost /var/www/facilpost-acme
if [[ ! -f /etc/facilpost/app.env ]]; then
  install -m 0600 /dev/null /etc/facilpost/app.env
  cat >/etc/facilpost/app.env <<ENV
PUBLIC_ORIGIN=https://$host
STATE_DIR=/var/lib/facilpost
META_APP_ID=
META_APP_SECRET=
ENV
fi
install -m 0755 "$sources/activate-backend.sh" /usr/local/libexec/facilpost-activate
cat >/etc/systemd/system/facilpost-api.service <<'UNIT'
[Unit]
Description=API do FácilPost
After=network.target

[Service]
User=facilpost-api
Group=facilpost-api
WorkingDirectory=/opt/facilpost/current
EnvironmentFile=/etc/facilpost/app.env
Environment=PYTHONDONTWRITEBYTECODE=1
StateDirectory=facilpost
StateDirectoryMode=0700
UMask=0077
ExecStart=/opt/facilpost/current/.venv/bin/uvicorn app:app --host 127.0.0.1 --port 8187 --no-access-log --proxy-headers --forwarded-allow-ips=127.0.0.1
Restart=on-failure
RestartSec=3
ProtectSystem=strict
ProtectHome=true
PrivateTmp=true
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
UNIT
cat >/etc/systemd/system/facilpost-release.service <<'UNIT'
[Unit]
Description=Ativar a revisão sincronizada do FácilPost
After=network-online.target facilpost-sync@backend.service

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/facilpost-activate
TimeoutStartSec=15min
UNIT
cat >/etc/systemd/system/facilpost-release.timer <<'UNIT'
[Unit]
Description=Verificar novas revisões do backend do FácilPost

[Timer]
OnBootSec=45s
OnUnitInactiveSec=60s
AccuracySec=5s

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable facilpost-api
systemctl start facilpost-release.service
systemctl enable --now facilpost-release.timer

config="/etc/nginx/sites-available/$host"
enabled="/etc/nginx/sites-enabled/$host"
marker='# Managed by FacilPost installer'
if [[ -e "$config" ]] && ! grep -Fxq "$marker" "$config"; then
  echo "Configuração já existente em $config. Revise antes de instalar." >&2
  exit 1
fi
if [[ -L "$enabled" || -e "$enabled" ]]; then
  [[ "$(readlink -f "$enabled")" == "$(readlink -f "$config")" ]] ||
    { echo "Entrada do nginx já existente em $enabled." >&2; exit 1; }
fi
work=$(mktemp -d)
trap 'rm -rf -- "$work"' EXIT

apply_nginx() {
  local existed=false linked=false
  if [[ -f "$config" ]]; then cp "$config" "$work/previous"; existed=true; fi
  [[ -L "$enabled" ]] && linked=true
  install -m 0644 "$work/site" "$config"
  [[ "$linked" == true ]] || ln -s "$config" "$enabled"
  if ! nginx -t || ! systemctl reload nginx; then
    if [[ "$existed" == true ]]; then cp "$work/previous" "$config"; else rm -f "$config"; fi
    [[ "$linked" == true ]] || rm -f "$enabled"
    return 1
  fi
}

certificate="/etc/letsencrypt/live/$host"
if [[ ! -s "$certificate/fullchain.pem" || ! -s "$certificate/privkey.pem" ]]; then
  cat >"$work/site" <<BLOCK
$marker
server {
    listen 80;
    listen [::]:80;
    server_name $host;
    location ^~ /.well-known/acme-challenge/ { root /var/www/facilpost-acme; }
    location / { return 404; }
}
BLOCK
  apply_nginx
  : "${CERTBOT_EMAIL:?Defina CERTBOT_EMAIL para emitir o certificado do subdomínio.}"
  certbot certonly --non-interactive --agree-tos --email "$CERTBOT_EMAIL" --webroot \
    -w /var/www/facilpost-acme --cert-name "$host" -d "$host"
fi
cat >"$work/site" <<BLOCK
$marker
server {
    listen 80;
    listen [::]:80;
    server_name $host;
    location ^~ /.well-known/acme-challenge/ { root /var/www/facilpost-acme; }
    location / { return 301 https://$host\$request_uri; }
}
server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name $host;
    ssl_certificate $certificate/fullchain.pem;
    ssl_certificate_key $certificate/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    root $base/frontend/public;
    index index.html;
    client_max_body_size 66m;
    location /api/ {
        proxy_pass http://127.0.0.1:8187;
        proxy_set_header Host \$host;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_read_timeout 300s;
        proxy_send_timeout 300s;
        access_log off;
    }
    location / { try_files \$uri \$uri/ /index.html; }
}
BLOCK
apply_nginx
install -d -m 0755 /etc/letsencrypt/renewal-hooks/deploy
cat >/etc/letsencrypt/renewal-hooks/deploy/facilpost-reload-nginx <<'HOOK'
#!/bin/sh
case " ${RENEWED_DOMAINS:-} " in
  *" facilpost.myfluxhub.tech "*) nginx -t && systemctl reload nginx ;;
esac
HOOK
chmod 0755 /etc/letsencrypt/renewal-hooks/deploy/facilpost-reload-nginx
curl --fail --silent --show-error --resolve "$host:443:127.0.0.1" "https://$host/api/health"
echo
echo "FácilPost disponível em https://$host/"
