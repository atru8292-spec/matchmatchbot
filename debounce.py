"""Debounce (склейка серии быстрых сообщений) — чистый in-memory таймер.

Отвечает ТОЛЬКО за «когда» обрабатывать номер, не за «что» (контент читается из БД
на флаше downstream-кодом). Поэтому модуль не зависит от db и чисто тестируется.

Логика (trailing debounce):
- пришло сообщение от номера → (пере)запускаем отсчёт `delay` секунд;
- новое сообщение в окне → сбрасываем отсчёт (ждём тишины после ПОСЛЕДНЕГО);
- тишина `delay` секунд → зовём on_flush(phone) РОВНО ОДИН раз;
- кэп `max_wait`: если лид строчит без пауз, флашим принудительно, чтобы не ждать вечно;
- сериализация по номеру: пока on_flush(phone) выполняется, второй параллельный флаш
  того же номера не стартует; пришедшие за это время сообщения дадут новый флаш после.

Состояние живёт в памяти процесса (1 сервер, десятки лидов/день — этого достаточно).
Контент сообщений уже сохранён в `messages` ДО debounce, так что рестарт в момент
склейки теряет максимум триггер обработки одного бурста, не данные.

БЭКЛОГ (не сейчас): «sweep непроцессенных inbound на старте» — подметать messages
с processed=false после рестарта. Пока просто корректно заполняем processed/processed_at,
чтобы потом было по чему подметать.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

logger = logging.getLogger("matchmatch.debounce")


class _PhoneState:
    """Состояние склейки одного номера."""
    __slots__ = ("timer", "first_ts", "flushing", "re_trigger")

    def __init__(self, first_ts: float):
        self.timer: asyncio.Task | None = None
        self.first_ts = first_ts        # время первого сообщения бурста (для max_wait)
        self.flushing = False           # идёт ли on_flush прямо сейчас
        self.re_trigger = False         # пришло ли новое сообщение во время on_flush


class Debouncer:
    """Per-phone trailing-debounce. on_flush — async callable(phone)."""

    def __init__(
        self,
        on_flush: Callable[[str], Awaitable[None]],
        delay: float = 4.0,
        max_wait: float = 15.0,
    ):
        self._on_flush = on_flush
        self._delay = delay
        self._max_wait = max_wait
        self._states: dict[str, _PhoneState] = {}

    async def trigger(self, phone: str) -> None:
        """Зарегистрировать входящее сообщение номера и (пере)запустить таймер."""
        now = asyncio.get_running_loop().time()
        state = self._states.get(phone)

        if state is None:
            # новый бурст
            state = _PhoneState(first_ts=now)
            self._states[phone] = state
            self._start_timer(phone, state)
            return

        if state.flushing:
            # флаш этого номера уже идёт — не стартуем второй, пометим на добор
            state.re_trigger = True
            return

        # активный бурст: сбрасываем таймер (ждём тишины после последнего сообщения)
        self._start_timer(phone, state)

    def _start_timer(self, phone: str, state: _PhoneState) -> None:
        """Отменить прежний таймер и поставить новый."""
        if state.timer is not None:
            state.timer.cancel()
        state.timer = asyncio.ensure_future(self._timer(phone, state))

    async def _timer(self, phone: str, state: _PhoneState) -> None:
        """Подождать delay (но не дольше max_wait от начала бурста) и флашнуть."""
        try:
            now = asyncio.get_running_loop().time()
            remaining_cap = self._max_wait - (now - state.first_ts)
            sleep_for = min(self._delay, remaining_cap)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            return  # таймер сброшен новым сообщением — выходим тихо
        # Защитный инвариант: флашить имеет право только таймер, который СЕЙЧАС
        # зарегистрирован в state. Если _start_timer уже поставил новый таймер
        # (крайне редкое окно отмены), этот, устаревший, выходит молча. Так
        # корректность не зависит от тонкой семантики cancel()/_must_cancel и loop
        # (проверено: на CPython и uvloop гонка не воспроизводится, но guard дешёв).
        if state.timer is not asyncio.current_task():
            return
        await self._flush(phone, state)

    async def _flush(self, phone: str, state: _PhoneState) -> None:
        """Вызвать on_flush(phone) один раз, с защитой от параллельного флаша номера."""
        # flushing=True ставим ДО await — так trigger() не запустит второй флаш
        state.flushing = True
        state.timer = None
        try:
            await self._on_flush(phone)
        except Exception:
            # ошибку не проглатываем молча — лог (алерт в Telegram навесит блок эскалации)
            logger.exception("debounce: on_flush упал для phone=%s", phone)
        finally:
            if state.re_trigger:
                # за время обработки пришли новые сообщения → новый бурст
                state.re_trigger = False
                state.flushing = False
                state.first_ts = asyncio.get_running_loop().time()
                self._start_timer(phone, state)
            else:
                self._states.pop(phone, None)

    async def shutdown(self) -> None:
        """Погасить все таймеры (MVP: молча; недосклеенный бурст подхватит следующий inbound)."""
        timers = [s.timer for s in self._states.values() if s.timer is not None]
        for t in timers:
            t.cancel()
        for t in timers:
            try:
                await t
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("debounce: ошибка таймера при shutdown")
        self._states.clear()
        logger.info("debounce: shutdown, таймеры погашены")
