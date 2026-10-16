# Деплой и восстановление MatchMatch-бота

Сервер: **XorekCloud VDS DE-E-2 #2031741**, Ubuntu 22.04, IP **2.26.71.254**,
хостнейм **2-26-71-254.sslip.io** (sslip.io указывает на IP, Let's Encrypt выдаёт валидный cert).
Архитектура: `Интернет :443 (TLS) → nginx → uvicorn 127.0.0.1:8000 (--no-access-log)`.
Мини-CRM отдаёт сам FastAPI из `miniapp/dist` по `/app/`. База и медиа во внешнем Supabase.

> ⚠️ Старый сервер 64.188.119.94 удалён хостером за неоплату (2026-10), диск и `.env`
> пропали. Этот IP мог достаться чужому клиенту: **не заходить туда и ничего не отправлять.**
>
> ⚠️ **Оплачивать XorekCloud вовремя и включить автопродление** в панели хостера.
> Повторение = снова поднимать всё с нуля.

Файлы в `deploy/`:
| Файл | Что это |
|---|---|
| `restore.sh` | восстановление на чистом сервере (пакеты, swap, ufw, код, venv, фронт, systemd, nginx, TLS) |
| `matchmatch-bot.service` | systemd-юнит бота (пользователь `matchmatch`) |
| `nginx-matchmatch.conf` | шаблон nginx, `__DOMAIN__` подставляет `restore.sh` |
| `healthcheck.sh` + `matchmatch-healthcheck.{service,timer}` | раз в 2 мин проверка сервиса и `/health`, алерт в техчат |
| `matchmatch-telegram-testbot.service` | тестовый TG-бот, **на проде не ставим** |

---

## Восстановление за 15 минут (новый сервер)

### 0. Доступ
- В панели хостера сверить отпечаток ключа сервера при первом `ssh root@<IP>`.
- Вход только по SSH-ключу. Если просит пароль или пишет «REMOTE HOST IDENTIFICATION HAS
  CHANGED», остановиться и разобраться, не обходить.

### 1. Система (на сервере, root)
```bash
apt-get update && apt-get install -y git
# restore.sh ещё не на сервере — скопировать с мака:
#   scp deploy/restore.sh root@<IP>:/root/restore.sh
STAGE=system bash /root/restore.sh
```

### 2. Deploy key (read-only) и клон
```bash
ssh-keygen -t ed25519 -N '' -C "matchmatch-deploy@$(hostname)" -f /root/.ssh/matchmatch_deploy
cat /root/.ssh/matchmatch_deploy.pub     # публичная часть → GitHub → репозиторий →
                                         # Settings → Deploy keys → Add (БЕЗ Allow write)
cat >> /root/.ssh/config <<'EOF'
Host github-matchmatch
  HostName github.com
  User git
  IdentityFile /root/.ssh/matchmatch_deploy
  IdentitiesOnly yes
EOF
chmod 600 /root/.ssh/config
ssh-keyscan -t ed25519 github.com >> /root/.ssh/known_hosts
# сверить: ssh-keygen -lf <(ssh-keyscan -t ed25519 github.com) ==
# SHA256:+DiY3wvvV6TuJJhbpZisF/zLDA0zPMSvHdkr4UvCOqU (официальный отпечаток GitHub)
git clone git@github-matchmatch:atru8292-spec/matchmatchbot.git /opt/matchmatch-bot
```

### 3. Секреты (с мака, ничего не печатая)
```bash
scp .env root@<IP>:/opt/matchmatch-bot/.env
scp google-service-account.json root@<IP>:/opt/matchmatch-bot/   # если есть
```
`restore.sh` сам ставит владельца `matchmatch` и права 600.
Имена переменных `.env` (значения — только в `.env`, никогда в git):
`WAZZUP_WEBHOOK_SECRET WAZZUP_TOKEN WAZZUP_CHANNEL_ID SUPABASE_URL SUPABASE_DB_DSN
SUPABASE_SERVICE_KEY OPENAI_API_KEY TG_MANAGER_BOT_TOKEN TG_MANAGER_CHAT_ID TG_MANAGER_ADMIN_IDS
TG_ALERTS_BOT_TOKEN TG_ALERTS_CHAT_ID TG_WEBHOOK_SECRET SILENT_BYPASS_PHONES
GOOGLE_CALENDAR_ID GOOGLE_SHEET_ID GOOGLE_GUEST_SHEET_ID MINI_APP_URL`
(`MINI_APP_URL=https://<домен>/app/`; если не задан — берётся из `config.py`).

### 4. Всё остальное
```bash
bash /opt/matchmatch-bot/deploy/restore.sh
```
Скрипт: venv + `pip install`, `npm ci && npm run build`, systemd (бот + healthcheck-таймер),
nginx, сертификат certbot, в конце `curl https://<домен>/health`.

### 5. SSH только по ключу (после проверки входа по ключу в ОТДЕЛЬНОЙ сессии)
```bash
cat > /etc/ssh/sshd_config.d/00-keys-only.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
sshd -t && systemctl reload ssh
```

### 6. Проверки (только чтение)
```bash
systemctl is-active matchmatch-bot
journalctl -u matchmatch-bot -n 50 --no-pager | grep -c Traceback    # 0
curl -s https://<домен>/health                                        # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' https://<домен>/app/         # 200
# эндпоинт вебхука: тестовый пинг Wazzup, бот ничего не отправляет
S=$(grep '^WAZZUP_WEBHOOK_SECRET=' /opt/matchmatch-bot/.env | cut -d= -f2-)
curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' \
  -d '{"test":true}' "http://127.0.0.1:8000/webhook/wazzup/$S"       # 200
```
Флаги в базе (`app_settings`): `bot_paused`, `scheduler_enabled`, `event_active` — посмотреть,
**не менять** при деплое.

### 7. Вебхуки (запись во внешние сервисы — только осознанно, сначала GET)
```bash
cd /opt/matchmatch-bot
env_get() { grep "^$1=" .env | cut -d= -f2-; }
BASE=https://<домен>

# Wazzup: текущее значение
curl -s -H "Authorization: Bearer $(env_get WAZZUP_TOKEN)" https://api.wazzup24.com/v3/webhooks
# Wazzup: установить
curl -s -X PATCH https://api.wazzup24.com/v3/webhooks \
  -H "Authorization: Bearer $(env_get WAZZUP_TOKEN)" -H "Content-Type: application/json" \
  -d "{\"webhooksUri\":\"$BASE/webhook/wazzup/$(env_get WAZZUP_WEBHOOK_SECRET)\",\"subscriptions\":{\"messagesAndStatuses\":true,\"contactsAndDealsCreation\":false}}"

# Telegram (бот «Лиды»): текущее значение
curl -s "https://api.telegram.org/bot$(env_get TG_MANAGER_BOT_TOKEN)/getWebhookInfo"
# Telegram: установить вебхук и кнопку меню Mini App
curl -s -X POST "https://api.telegram.org/bot$(env_get TG_MANAGER_BOT_TOKEN)/setWebhook" \
  -H "Content-Type: application/json" \
  -d "{\"url\":\"$BASE/webhook/telegram/$(env_get TG_WEBHOOK_SECRET)\",\"allowed_updates\":[\"message\",\"callback_query\"],\"drop_pending_updates\":true}"
curl -s -X POST "https://api.telegram.org/bot$(env_get TG_MANAGER_BOT_TOKEN)/setChatMenuButton" \
  -H "Content-Type: application/json" \
  -d "{\"menu_button\":{\"type\":\"web_app\",\"text\":\"CRM\",\"web_app\":{\"url\":\"$BASE/app/\"}}}"
```
Ответы этих команд содержат URL с секретом — не копировать их в чаты и тикеты.
Адрес Mini App в BotFather (если задан там) поменять руками.

---

## Обновление кода
```bash
cd /opt/matchmatch-bot && git pull --ff-only origin main \
  && venv/bin/pip install -q -r requirements.txt \
  && (cd miniapp && npm ci --no-audit --no-fund && npm run build) \
  && systemctl restart matchmatch-bot && git log -1 --oneline
```
Рестарт обязателен: промпт и сценарии кэшируются в памяти процесса.

## Операции
```bash
systemctl restart|stop|status matchmatch-bot
journalctl -u matchmatch-bot -n 200 --no-pager
journalctl -t matchmatch-healthcheck -n 20      # история алертов healthcheck
systemctl list-timers matchmatch-healthcheck.timer
certbot renew --dry-run                          # сертификат продлевается certbot.timer
```

## Бэкап секретов (НЕ в репозиторий)
- `.env` и `google-service-account.json` хранить в менеджере паролей (1Password/Bitwarden,
  вложение) или на зашифрованном диске. Локальный `.env` на маке — не бэкап: он может
  отличаться от боевого.
- После каждой смены ключа (перевыпуск токена, новый пароль Supabase) — обновить копию.
- Проверка, что копия актуальна, без вывода значений: сравнить хэши
  `sha256sum .env` на сервере и у копии.

## Миграции БД
База внешняя (Supabase), при переезде сервера миграции НЕ нужны. Новые SQL из `migrations/`
применять только осознанно, после просмотра SQL.
