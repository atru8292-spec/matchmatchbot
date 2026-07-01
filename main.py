"""FastAPI-сервер бота Anna.

Блок 1 (скелет): приём вебхука Wazzup24 + health-check.
Блок 2: пул БД поднимается/закрывается через lifespan.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response

import db
from config import settings

# Логи в stdout → journald (systemd). Помечаем время/уровень/модуль.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("matchmatch")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Жизненный цикл: поднять пул БД при старте, закрыть при остановке.

    Если DSN не задан — работаем без БД (пул не создаётся), чтобы приём вебхука
    поднимался и без настроенной базы. Как только БД станет обязательной на
    горячем пути — сделаем DSN строго обязательным.
    """
    if settings.supabase_db_dsn:
        await db.init_pool()
    else:
        logger.warning("SUPABASE_DB_DSN не задан — БД не подключена")
    yield
    await db.close_pool()


app = FastAPI(title="MatchMatch Anna Bot", lifespan=lifespan)


@app.get("/health")
async def health():
    """Проверка живости для systemd/мониторинга."""
    return {"status": "ok"}


@app.post("/webhook/wazzup/{secret}")
async def wazzup_webhook(secret: str, request: Request):
    """Приём вебхука Wazzup24.

    - Сверяем секрет в пути (у Wazzup нет подписи — это наша защита).
    - Отвечаем 200 на тестовый пинг {test: true}.
    - Логируем входящие сообщения и статусы доставки.
    - ВСЕГДА возвращаем 200, даже при ошибке обработки (иначе Wazzup уходит
      в ретрай-шторм). Ошибку логируем ПЕРЕД возвратом, чтобы видеть её.

    ВНИМАНИЕ (деплой): секрет — часть URL, поэтому он попадёт в access-log
    uvicorn. На проде запускать с `--no-access-log` (см. systemd-юнит, блок 12),
    иначе секрет утечёт в journald.
    """
    # Неверный секрет — единственный случай не-200 (это не Wazzup, а чужой запрос).
    if secret != settings.wazzup_webhook_secret:
        logger.warning("Webhook: неверный секрет в пути")
        raise HTTPException(status_code=403, detail="Forbidden")

    # Парсинг тела. Битый JSON не должен ронять эндпоинт.
    try:
        body = await request.json()
    except Exception:
        raw = await request.body()
        logger.error("Webhook: не смог распарсить JSON, тело=%r", raw[:500])
        return Response(status_code=200)

    try:
        # Тестовый пинг при подключении вебхука в кабинете Wazzup.
        if isinstance(body, dict) and body.get("test") is True:
            logger.info("Webhook: тестовый пинг {test:true} — OK")
            return Response(status_code=200)

        messages = body.get("messages") if isinstance(body, dict) else None
        statuses = body.get("statuses") if isinstance(body, dict) else None

        if messages:
            for msg in messages:
                _log_incoming_message(msg)

        if statuses:
            # Статусы доставки/прочтения обрабатывать будем позже — пока только счётчик.
            logger.info("Webhook: статусов доставки: %d (пока игнор)", len(statuses))

        if not messages and not statuses:
            logger.info("Webhook: пейлоад без messages/statuses: %r", str(body)[:300])

    except Exception:
        # Любая ошибка обработки — логируем стектрейс, но отвечаем 200.
        logger.exception("Webhook: ошибка обработки пейлоада")

    return Response(status_code=200)


def _log_incoming_message(msg) -> None:
    """Кратко логируем входящее сообщение.

    Помечаем channelId и type (text/image/audio/video/document) — пригодится
    при отладке нормализации в блоке 2.
    """
    if not isinstance(msg, dict):
        logger.info("Webhook: элемент messages не dict: %r", str(msg)[:200])
        return
    logger.info(
        "Webhook inbound: channelId=%s type=%s chatType=%s chatId=%s "
        "isEcho=%s status=%s has_text=%s has_media=%s messageId=%s",
        msg.get("channelId"),
        msg.get("type"),
        msg.get("chatType"),
        msg.get("chatId"),
        msg.get("isEcho"),
        msg.get("status"),
        bool(msg.get("text")),
        bool(msg.get("contentUri")),
        msg.get("messageId"),
    )
