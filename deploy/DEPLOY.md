# Деплой MatchMatch-бота на сервер (блок 12)

Сервер: **64.188.119.94** · хостнейм: **64-188-119-94.sslip.io** (sslip.io → указывает на IP, Let's Encrypt выдаёт валидный cert).
Архитектура: `Интернет :443 (TLS) → nginx → uvicorn 127.0.0.1:8000 (--no-access-log)`.
localtunnel больше НЕ используется.

Все команды — на сервере под root (или через `sudo`). Пути: код в `/opt/matchmatch-bot`, процесс под юзером `matchmatch`.

---

## 1. Пакеты
```bash
apt update
apt install -y python3.11 python3.11-venv git nginx certbot python3-certbot-nginx ufw ffmpeg  # ffmpeg — сжатие видео с ивентов (mini_api /event/media)
```

## 2. Пользователь и код
```bash
useradd --system --create-home --shell /usr/sbin/nologin matchmatch
mkdir -p /opt/matchmatch-bot
git clone <REPO_URL> /opt/matchmatch-bot     # или залить код иначе
cd /opt/matchmatch-bot
python3.11 -m venv venv
venv/bin/pip install --upgrade pip
venv/bin/pip install -r requirements.txt
chown -R matchmatch:matchmatch /opt/matchmatch-bot
```

## 3. .env (боевой, НЕ из git)
Скопировать локальный `.env` на сервер и выставить БОЕВЫЕ значения (см. PRODUCTION_CHECKLIST.md):
```bash
# с локальной машины:
scp .env root@64.188.119.94:/opt/matchmatch-bot/.env
# на сервере:
chown matchmatch:matchmatch /opt/matchmatch-bot/.env
chmod 600 /opt/matchmatch-bot/.env
```
Проверить в .env:
- `TG_MANAGER_CHAT_ID` = chat_id **Ани** · `TG_MANAGER_ADMIN_IDS` = id Ани (+ разработки)
- `SILENT_BYPASS_PHONES` — **пусто** (убрать тестовые +7)
- `WAZZUP_WEBHOOK_SECRET`, `TG_WEBHOOK_SECRET` — длинные случайные
- `SUPPORT_CONTACT=@arinashrr`
- Supabase DSN / OpenAI / Wazzup / оба TG-токена — прод

## 3.5. Миграции БД
Применить SQL-миграции к боевой БД по порядку (idempotent, `IF NOT EXISTS`/`ON CONFLICT`):
```bash
for f in migrations/0*.sql; do
  echo ">> $f"; psql "$SUPABASE_DB_DSN" -f "$f"   # или через Supabase SQL editor
done
```
`004_block13` создаёт `app_settings` и системный сценарий 50. ВНИМАНИЕ: если будешь
пере-заливать сценарии из JSON (`scripts.load_scenarios` делает DELETE) — после этого
повторно применить `004` (вернёт сценарий 50 и его trigger_type='scheduled').

## 4. systemd
```bash
cp deploy/matchmatch-bot.service /etc/systemd/system/matchmatch-bot.service
systemctl daemon-reload
systemctl enable --now matchmatch-bot
systemctl status matchmatch-bot          # active (running)
curl -s http://127.0.0.1:8000/health     # {"status":"ok"}
journalctl -u matchmatch-bot -f          # логи (следить)
```

## 5. nginx + TLS
```bash
cp deploy/nginx-matchmatch.conf /etc/nginx/sites-available/matchmatch
ln -sf /etc/nginx/sites-available/matchmatch /etc/nginx/sites-enabled/matchmatch
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
# выпуск сертификата (certbot сам добавит 443 ssl + редирект 80→443):
certbot --nginx -d 64-188-119-94.sslip.io --non-interactive --agree-tos -m arinashrr@gmail.com
nginx -t && systemctl reload nginx
curl -s https://64-188-119-94.sslip.io/health   # {"status":"ok"} по HTTPS
```

## 6. Firewall
```bash
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
# порт 8000 наружу НЕ открываем (uvicorn слушает только 127.0.0.1)
```

## 7. Переключить вебхуки на постоянный URL
```bash
# значения берём из .env на сервере
WSECRET=$(grep '^WAZZUP_WEBHOOK_SECRET=' .env | cut -d= -f2)
WTOKEN=$(grep '^WAZZUP_TOKEN=' .env | cut -d= -f2)
TSECRET=$(grep '^TG_WEBHOOK_SECRET=' .env | cut -d= -f2)
TTOKEN=$(grep '^TG_MANAGER_BOT_TOKEN=' .env | cut -d= -f2)
BASE=https://64-188-119-94.sslip.io

# Wazzup
curl -s -X PATCH https://api.wazzup24.com/v3/webhooks \
  -H "Authorization: Bearer $WTOKEN" -H "Content-Type: application/json" \
  -d "{\"webhooksUri\":\"$BASE/webhook/wazzup/$WSECRET\",\"subscriptions\":{\"messagesAndStatuses\":true,\"contactsAndDealsCreation\":false}}"

# Telegram (бот «Лиды»)
curl -s -X POST "https://api.telegram.org/bot$TTOKEN/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"$BASE/webhook/telegram/$TSECRET\",\"allowed_updates\":[\"message\",\"callback_query\"],\"drop_pending_updates\":true}"
curl -s "https://api.telegram.org/bot$TTOKEN/getWebhookInfo"   # url верный, last_error пустой
```

## 8. Whitelist клиентов (перед боевым приёмом лидов)
Когда Аня пришлёт список VIP/текущих клиентов — залить их номера:
```bash
venv/bin/python - <<'PY'
import asyncio, db
async def main():
    await db.init_pool()
    for phone, who in [("+52 55 ...", "клиент"), ...]:
        await db.add_to_whitelist(phone, who, "deploy")
    await db.close_pool()
asyncio.run(main())
PY
```

## 9. Прод smoke-тест
- `curl https://64-188-119-94.sslip.io/health` → 200
- WhatsApp: написать боту с НЕ-whitelist номера → бот отвечает (или молчит по правилам)
- Telegram боту «Лиды»: `/help` → ответ; `/leads` → список
- `journalctl -u matchmatch-bot -f` — без ошибок/traceback

## Операции
```bash
systemctl restart matchmatch-bot      # рестарт (startup-sweep сам добьёт непроцессенные)
systemctl stop matchmatch-bot         # стоп
journalctl -u matchmatch-bot -n 200   # последние логи
# обновление кода:
cd /opt/matchmatch-bot && git pull && venv/bin/pip install -r requirements.txt \
  && systemctl restart matchmatch-bot
```
Сертификат sslip.io продлевается автоматически (`certbot.timer`). Проверка: `certbot renew --dry-run`.
