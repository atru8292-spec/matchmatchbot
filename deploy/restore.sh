#!/usr/bin/env bash
# Восстановление MatchMatch-бота на чистом Ubuntu 22.04 (под root).
# Идемпотентен: можно запускать повторно, уже сделанные шаги пропускаются.
#
# Чего скрипт НЕ делает (руками, см. deploy/DEPLOY.md):
#   - не создаёт deploy key и не добавляет его в GitHub;
#   - не кладёт .env и google-service-account.json (секреты, только scp с мака);
#   - не отключает вход по паролю в SSH (сначала проверить вход по ключу);
#   - не трогает вебхуки Wazzup/Telegram и базу (bot_paused и пр.).
#
# Использование:
#   bash deploy/restore.sh                # домен берётся из публичного IP
#   DOMAIN=1-2-3-4.sslip.io bash deploy/restore.sh
#   STAGE=system bash deploy/restore.sh   # только пакеты/swap/ufw (до клонирования)
set -euo pipefail

APP_DIR=/opt/matchmatch-bot
APP_USER=matchmatch
REPO_SSH=git@github-matchmatch:atru8292-spec/matchmatchbot.git
CERT_EMAIL=${CERT_EMAIL:-arinashrr@gmail.com}
STAGE=${STAGE:-all}

log() { echo; echo ">>> $*"; }

[ "$(id -u)" = 0 ] || { echo "запускать под root"; exit 1; }

# ---------- 1. Система ----------
log "пакеты"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get upgrade -yq
apt-get install -yq git curl ca-certificates ufw fail2ban unattended-upgrades \
  nginx certbot python3-certbot-nginx ffmpeg build-essential software-properties-common

if ! command -v python3.11 >/dev/null; then
  log "Python 3.11 из deadsnakes"
  add-apt-repository -y ppa:deadsnakes/ppa
  apt-get update -q
fi
apt-get install -yq python3.11 python3.11-venv python3.11-dev

if ! command -v node >/dev/null || ! node -v | grep -q '^v20\.'; then
  log "Node.js 20 LTS (NodeSource)"
  curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
  apt-get install -yq nodejs
fi

log "часовой пояс UTC"
timedatectl set-timezone UTC

log "автообновления безопасности"
dpkg-reconfigure -f noninteractive unattended-upgrades
# На образе XorekCloud нет /var/log/auth.log (только journald) — без backend=systemd
# fail2ban падает с «Have not found any log file for sshd jail».
cat > /etc/fail2ban/jail.d/sshd-systemd.local <<'EOF'
[sshd]
enabled = true
backend = systemd
EOF
systemctl enable fail2ban
systemctl restart fail2ban

# swap 2 ГБ, если RAM < 4 ГБ и swap ещё нет
mem_kb=$(awk '/MemTotal/ {print $2}' /proc/meminfo)
if [ "$mem_kb" -lt 4000000 ] && ! swapon --show | grep -q .; then
  log "swap 2G"
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

log "ufw: только 22, 80, 443"
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable

[ "$STAGE" = system ] && { log "STAGE=system — готово"; exit 0; }

# ---------- 2. Код ----------
id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$APP_USER"

if [ ! -d "$APP_DIR/.git" ]; then
  log "клонирование (нужен deploy key, см. DEPLOY.md §2)"
  git clone "$REPO_SSH" "$APP_DIR"
else
  log "код уже есть — git pull"
  git -C "$APP_DIR" pull --ff-only origin main
fi

# Код принадлежит root (git pull под root), сервис читает его от matchmatch.
# Приложение пишет только во временные каталоги, право записи в код ему не нужно.
cd "$APP_DIR"
[ -d venv ] || python3.11 -m venv venv
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

log "сборка мини-CRM (miniapp/dist)"
(cd miniapp && npm ci --no-audit --no-fund && npm run build)

# ---------- 3. Секреты (только проверка наличия) ----------
missing=0
if [ ! -f "$APP_DIR/.env" ]; then
  echo "!!! нет $APP_DIR/.env — скопировать с мака (DEPLOY.md §3)"; missing=1
else
  chown "$APP_USER:$APP_USER" "$APP_DIR/.env"; chmod 600 "$APP_DIR/.env"
fi
if [ -f "$APP_DIR/google-service-account.json" ]; then
  chown "$APP_USER:$APP_USER" "$APP_DIR/google-service-account.json"
  chmod 600 "$APP_DIR/google-service-account.json"
else
  echo "--- нет google-service-account.json — бот работает, Calendar/Sheets нет"
fi

# ---------- 4. systemd ----------
log "systemd"
install -m 644 deploy/matchmatch-bot.service /etc/systemd/system/matchmatch-bot.service
install -m 644 deploy/matchmatch-healthcheck.service /etc/systemd/system/matchmatch-healthcheck.service
install -m 644 deploy/matchmatch-healthcheck.timer /etc/systemd/system/matchmatch-healthcheck.timer
systemctl daemon-reload
systemctl enable matchmatch-bot
if [ "$missing" = 0 ]; then
  systemctl restart matchmatch-bot
  sleep 5   # healthcheck включаем после старта бота, иначе первый прогон даст ложный алерт
  systemctl enable --now matchmatch-healthcheck.timer
else
  echo "!!! сервис не запущен: нет .env"
fi

# ---------- 5. nginx + TLS ----------
if [ -z "${DOMAIN:-}" ]; then
  ip=$(curl -fs4 https://api.ipify.org)
  DOMAIN="${ip//./-}.sslip.io"
fi
log "nginx для $DOMAIN"
sed "s/__DOMAIN__/$DOMAIN/" deploy/nginx-matchmatch.conf > /etc/nginx/sites-available/matchmatch
ln -sf /etc/nginx/sites-available/matchmatch /etc/nginx/sites-enabled/matchmatch
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx

if [ ! -d "/etc/letsencrypt/live/$DOMAIN" ]; then
  log "сертификат Let's Encrypt"
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$CERT_EMAIL" --redirect
else
  # nginx-конфиг перезаписан выше — вернуть в него блок 443 от certbot
  certbot install --nginx --cert-name "$DOMAIN" -d "$DOMAIN" --redirect --non-interactive
fi
nginx -t && systemctl reload nginx

log "проверка"
sleep 3
systemctl is-active matchmatch-bot || true
curl -fs "https://$DOMAIN/health" && echo || echo "!!! /health по HTTPS не ответил"
echo "Дальше: DEPLOY.md §7 (вебхуки — только вручную, после проверки GET-ом)."
