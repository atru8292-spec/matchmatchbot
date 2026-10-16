#!/usr/bin/env bash
# Проверка здоровья бота (запускается таймером matchmatch-healthcheck раз в 2 минуты).
# Сервис не active или /health не отвечает → алерт в техчат Telegram
# (тот же бот и чат, что у escalation.notify_error: TG_ALERTS_BOT_TOKEN / TG_ALERTS_CHAT_ID).
# Алерт один раз на падение и один раз на восстановление, без шторма.
set -uo pipefail

ENV_FILE=/opt/matchmatch-bot/.env
STATE_DIR=/var/lib/matchmatch-healthcheck
STATE_FILE=$STATE_DIR/state
mkdir -p "$STATE_DIR"

env_get() {
  # значение переменной из .env без вывода на экран; снимаем кавычки
  grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- | sed -e 's/^"//' -e 's/"$//' -e "s/^'//" -e "s/'$//"
}

problem=""
if ! systemctl is-active --quiet matchmatch-bot; then
  problem="сервис matchmatch-bot: $(systemctl is-active matchmatch-bot 2>/dev/null)"
elif ! curl -fs -m 10 http://127.0.0.1:8000/health >/dev/null; then
  problem="/health не отвечает на 127.0.0.1:8000"
fi

prev=$(cat "$STATE_FILE" 2>/dev/null || echo ok)
now=ok; [ -n "$problem" ] && now=down
[ "$now" = "$prev" ] && exit 0
echo "$now" > "$STATE_FILE"

if [ "$now" = down ]; then
  text="🔧 Бот упал ($(hostname), $(date -u '+%Y-%m-%d %H:%M') UTC): $problem"
else
  text="✅ Бот снова работает ($(hostname), $(date -u '+%Y-%m-%d %H:%M') UTC)"
fi
logger -t matchmatch-healthcheck "$text"

token=$(env_get TG_ALERTS_BOT_TOKEN)
chat=$(env_get TG_ALERTS_CHAT_ID)
if [ -z "$token" ] || [ -z "$chat" ]; then
  logger -t matchmatch-healthcheck "TG_ALERTS_* не заданы — алерт не отправлен"
  exit 0
fi
# токен передаём через stdin (-K -), чтобы он не светился в списке процессов
curl -s -m 15 -o /dev/null -K - --data-urlencode "chat_id=$chat" --data-urlencode "text=$text" <<EOF
url = "https://api.telegram.org/bot$token/sendMessage"
EOF
