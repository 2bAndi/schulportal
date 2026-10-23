#!/usr/bin/env bash
# Schulblick – first-time setup on the Tangosummer server (Ubuntu 24.04, host nginx, Docker Compose).
# Run from the project directory, e.g.:  cd /opt/schulblick && sudo bash deploy/setup.sh
set -euo pipefail

DOMAIN="${DOMAIN:-family.verstehst.de}"
APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER_UID=10001

say()  { printf '\n\033[1;32m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m!! %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31mxx %s\033[0m\n' "$*"; exit 1; }

[[ $EUID -eq 0 ]] || die "Bitte mit sudo ausführen."
command -v docker >/dev/null || die "docker fehlt"
docker compose version >/dev/null || die "docker compose fehlt"
command -v nginx >/dev/null || die "nginx fehlt auf dem Host"
cd "$APP_DIR"

say "1/6  DNS prüfen ($DOMAIN)"
SERVER_IP="$(curl -fsS https://api.ipify.org || true)"
DNS_IP="$(getent ahostsv4 "$DOMAIN" | awk 'NR==1{print $1}' || true)"
echo "Server: ${SERVER_IP:-?}   DNS $DOMAIN: ${DNS_IP:-nicht gefunden}"
if [[ -z "$DNS_IP" || "$DNS_IP" != "$SERVER_IP" ]]; then
  warn "Der A-Record von $DOMAIN zeigt (noch) nicht auf diesen Server."
  read -rp "Trotzdem weitermachen? [j/N] " ok; [[ "$ok" == "j" ]] || exit 1
fi

say "2/6  Zugangsdaten Schulportal (Elternkonto) als Docker-Secret"
mkdir -p secrets && chmod 700 secrets
if [[ ! -s secrets/sph_credentials ]]; then
  read -rp  "Benutzername im Schulportal (ohne Schulnummer, z. B. vorname.nachname): " SPH_USER
  read -rsp "Passwort im Schulportal: " SPH_PASS; echo
  umask 077
  printf 'username=%s\npassword=%s\n' "$SPH_USER" "$SPH_PASS" > secrets/sph_credentials
  unset SPH_PASS
else
  echo "secrets/sph_credentials existiert bereits – unverändert gelassen."
fi
chown "$CONTAINER_UID:$CONTAINER_UID" secrets/sph_credentials
chmod 400 secrets/sph_credentials
# optional AI key (empty file = AI off); set it later with deploy/ki-schluessel.sh
if [[ ! -e secrets/anthropic_key ]]; then
  install -m 400 -o "$CONTAINER_UID" -g "$CONTAINER_UID" /dev/null secrets/anthropic_key
fi

say "3/6  Container bauen und starten"
# the container runs as uid 10001 and must be able to read the config
chmod 755 config && chmod 644 config/*.toml
docker compose build
docker compose up -d
sleep 5
docker compose ps
curl -fsS http://127.0.0.1:8130/healthz >/dev/null && echo "Web-App antwortet auf 127.0.0.1:8130"

say "4/6  Anmeldung am Schulportal testen"
docker compose run --rm worker schulblick check-login || warn "Login fehlgeschlagen – secrets/sph_credentials prüfen, dann: docker compose restart worker"

say "5/6  nginx und Zertifikat"
install -m 644 deploy/nginx/schulblick-ratelimit.conf /etc/nginx/conf.d/schulblick-ratelimit.conf
install -d /etc/nginx/snippets
install -m 644 deploy/nginx/schulblick-proxy.conf /etc/nginx/snippets/schulblick-proxy.conf
if [[ ! -f /etc/letsencrypt/live/$DOMAIN/fullchain.pem ]]; then
  command -v certbot >/dev/null || die "certbot fehlt (apt install certbot python3-certbot-nginx)"
  install -m 644 deploy/nginx/family.verstehst.de.bootstrap.conf /etc/nginx/sites-available/$DOMAIN
  ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
  mkdir -p /var/www/html
  nginx -t && systemctl reload nginx
  certbot certonly --webroot -w /var/www/html -d "$DOMAIN" --non-interactive --agree-tos \
          --register-unsafely-without-email --keep-until-expiring || die "certbot fehlgeschlagen"
fi
install -m 644 deploy/nginx/family.verstehst.de.conf /etc/nginx/sites-available/$DOMAIN
# older certbot setups may lack the shared TLS snippets – drop those two lines then
[[ -f /etc/letsencrypt/options-ssl-nginx.conf ]] || sed -i '/options-ssl-nginx.conf/d' /etc/nginx/sites-available/$DOMAIN
[[ -f /etc/letsencrypt/ssl-dhparams.pem ]] || sed -i '/ssl-dhparams.pem/d' /etc/nginx/sites-available/$DOMAIN
ln -sf /etc/nginx/sites-available/$DOMAIN /etc/nginx/sites-enabled/$DOMAIN
nginx -t && systemctl reload nginx

say "6/6  Fertig – jetzt Passwörter setzen"
cat <<EOF
Für jedes Konto einmal (Eingabe ist unsichtbar):
  docker compose run --rm web schulblick set-password papa
  docker compose run --rm web schulblick set-password mama
  docker compose run --rm web schulblick set-password ben
  docker compose run --rm web schulblick set-password boje
  docker compose run --rm web schulblick set-password lotte

Zweiter Faktor für die Eltern (QR-Code mit der Authenticator-App scannen):
  docker compose run --rm web schulblick totp-setup papa
  docker compose run --rm web schulblick totp-setup mama

Übersicht:   docker compose run --rm web schulblick users
Aufrufen:    https://$DOMAIN
Logs:        docker compose logs -f worker
EOF
