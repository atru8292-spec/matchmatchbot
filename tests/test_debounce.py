"""Тесты debounce.py — Блок 4.

Async-тесты (asyncio_mode=auto в pytest.ini подхватывает async def автоматически).
Малые задержки: delay~0.05–0.08s, max_wait~0.2–0.5s — весь набор проходит за ~5–8s.
Паузы берём с запасом (delay * 3+) чтобы не было флаки-тестов на медленных машинах.
"""
from __future__ import annotations

import asyncio
from typing import List, Tuple

import pytest

from debounce import Debouncer


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

def make_log_flush() -> Tuple[List[Tuple[str, float]], object]:
    """Возвращает лог [(phone, loop_time)] и on_flush, который в него пишет."""
    log: List[Tuple[str, float]] = []

    async def on_flush(phone: str) -> None:
        log.append((phone, asyncio.get_running_loop().time()))

    return log, on_flush


# ===========================================================================
# 1. Один trigger → ровно 1 флаш, не раньше delay
# ===========================================================================

class TestSingleTrigger:
    async def test_one_trigger_yields_one_flush(self):
        """Один trigger → ровно 1 флаш."""
        log, on_flush = make_log_flush()
        d = Debouncer(on_flush, delay=0.05, max_wait=0.3)

        await d.trigger("p1")
        assert len(log) == 0, "Флаш случился до delay!"

        await asyncio.sleep(0.18)  # 3.6 * delay — достаточный запас
        assert len(log) == 1
        assert log[0][0] == "p1"

    async def test_no_flush_before_delay_elapses(self):
        """До истечения delay флаша нет (проверяем в середине окна)."""
        log, on_flush = make_log_flush()
        delay = 0.08
        d = Debouncer(on_flush, delay=delay, max_wait=0.5)

        await d.trigger("p1")

        await asyncio.sleep(delay * 0.4)  # ещё не истёк delay
        assert len(log) == 0, "Флаш случился ДО delay!"

        await asyncio.sleep(delay * 2.5)  # теперь уже после
        assert len(log) == 1

    async def test_flush_time_not_before_delay(self):
        """loop_time флаша >= trigger_time + delay (trailing, не leading)."""
        log, on_flush = make_log_flush()
        delay = 0.06
        d = Debouncer(on_flush, delay=delay, max_wait=0.5)

        t0 = asyncio.get_running_loop().time()
        await d.trigger("p1")
        await asyncio.sleep(delay * 4)

        assert len(log) == 1
        elapsed = log[0][1] - t0
        assert elapsed >= delay * 0.8, f"Флаш слишком рано: elapsed={elapsed:.4f}s, delay={delay}"


# ===========================================================================
# 2. Бурст → ровно 1 флаш (склейка), trailing после последнего
# ===========================================================================

class TestBurst:
    async def test_burst_five_messages_one_flush(self):
        """5 trigger подряд с паузой << delay → ровно 1 флаш."""
        log, on_flush = make_log_flush()
        delay = 0.08
        d = Debouncer(on_flush, delay=delay, max_wait=0.5)

        for _ in range(5):
            await d.trigger("p1")
            await asyncio.sleep(0.01)  # 0.01 << delay=0.08

        assert len(log) == 0, "Флаш случился ещё в ходе бурста!"

        await asyncio.sleep(delay * 3)
        assert len(log) == 1

    async def test_burst_trailing_after_last_message(self):
        """Флаш trailing — loop_time >= t_last_trigger + delay."""
        log, on_flush = make_log_flush()
        delay = 0.06
        d = Debouncer(on_flush, delay=delay, max_wait=0.5)

        await d.trigger("p1")
        await asyncio.sleep(0.015)
        await d.trigger("p1")
        t_last = asyncio.get_running_loop().time()

        await asyncio.sleep(delay * 4)

        assert len(log) == 1
        assert log[0][1] >= t_last + delay * 0.8, (
            f"Флаш пришёл до t_last+delay: flush_t={log[0][1]:.4f}, "
            f"t_last+delay={t_last + delay:.4f}"
        )


# ===========================================================================
# 3. Сброс таймера: повторный trigger сдвигает момент флаша
# ===========================================================================

class TestTimerReset:
    async def test_second_trigger_resets_timer(self):
        """trigger → пауза < delay → trigger: ровно 1 флаш, после второго trigger."""
        log, on_flush = make_log_flush()
        delay = 0.08
        d = Debouncer(on_flush, delay=delay, max_wait=0.5)

        await d.trigger("p1")
        await asyncio.sleep(delay * 0.5)  # < delay, флаша ещё нет
        assert len(log) == 0

        await d.trigger("p1")             # сброс таймера
        t_second = asyncio.get_running_loop().time()

        # Сразу после сброса — нового delay ещё не прошло
        await asyncio.sleep(delay * 0.5)
        assert len(log) == 0, "Флаш сразу после сброса — таймер не сбросился?"

        await asyncio.sleep(delay * 2.5)
        assert len(log) == 1
        # Флаш не раньше t_second + delay
        assert log[0][1] >= t_second + delay * 0.8, (
            f"Флаш слишком рано после сброса: flush_t={log[0][1]:.4f}, "
            f"t_second+delay={t_second + delay:.4f}"
        )


# ===========================================================================
# 4. Два разных номера → два независимых флаша
# ===========================================================================

class TestTwoPhones:
    async def test_two_phones_both_flush(self):
        """Два номера — каждый флашится по одному разу."""
        log, on_flush = make_log_flush()
        d = Debouncer(on_flush, delay=0.05, max_wait=0.3)

        await d.trigger("pA")
        await d.trigger("pB")

        await asyncio.sleep(0.22)  # > delay * 3

        phones = {e[0] for e in log}
        assert len(log) == 2
        assert phones == {"pA", "pB"}

    async def test_two_phones_timers_are_independent(self):
        """Таймеры двух номеров не влияют друг на друга."""
        log, on_flush = make_log_flush()
        delay = 0.05
        d = Debouncer(on_flush, delay=delay, max_wait=0.3)

        await d.trigger("pA")
        await asyncio.sleep(delay * 0.8)  # чуть позже — trigger pB
        await d.trigger("pB")

        await asyncio.sleep(delay * 4)

        assert len(log) == 2
        times = {e[0]: e[1] for e in log}
        # pA запустился раньше → флашится раньше
        assert times["pA"] < times["pB"], (
            f"pA флашнулся позже pB: {times['pA']:.4f} vs {times['pB']:.4f}"
        )


# ===========================================================================
# 5. max_wait кэп: флаш случается ВО ВРЕМЯ непрерывного потока
# ===========================================================================

class TestMaxWait:
    async def test_max_wait_fires_during_continuous_stream(self):
        """Непрерывный поток trigger с interval < delay → флаш ~max_wait от старта."""
        flush_times: list[float] = []
        flush_event = asyncio.Event()

        async def on_flush(phone: str) -> None:
            flush_times.append(asyncio.get_running_loop().time())
            flush_event.set()

        delay = 0.08
        max_wait = 0.2
        interval = 0.025  # << delay, таймер постоянно сбрасывается
        d = Debouncer(on_flush, delay=delay, max_wait=max_wait)

        t0 = asyncio.get_running_loop().time()

        async def continuous_stream():
            # 40 сообщений × 0.025s = 1.0s >> max_wait — поток длиннее max_wait
            for _ in range(40):
                await d.trigger("p1")
                await asyncio.sleep(interval)

        stream_task = asyncio.ensure_future(continuous_stream())

        # Ждём max_wait * 2 — флаш должен произойти ВО ВРЕМЯ потока (не после)
        await asyncio.sleep(max_wait * 2.0)

        # Останавливаем поток
        stream_task.cancel()
        try:
            await stream_task
        except asyncio.CancelledError:
            pass

        await d.shutdown()

        assert len(flush_times) >= 1, (
            "max_wait не сработал: ни одного флаша за время непрерывного потока"
        )
        elapsed = flush_times[0] - t0
        assert elapsed <= max_wait * 2.5, (
            f"Первый флаш пришёл слишком поздно: {elapsed:.3f}s > max_wait*2.5={max_wait*2.5}"
        )


# ===========================================================================
# 6. Сериализация по номеру
# ===========================================================================

class TestSerialization:
    async def test_no_parallel_flushes_for_same_phone(self):
        """Параллельных on_flush для одного номера не бывает (max concurrent == 1)."""
        concurrent = 0
        max_concurrent = 0
        flush_started = asyncio.Event()

        async def slow_flush(phone: str) -> None:
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            flush_started.set()
            await asyncio.sleep(0.1)  # медленная обработка
            concurrent -= 1

        delay = 0.05
        d = Debouncer(slow_flush, delay=delay, max_wait=0.5)

        # Первый trigger → запустит флаш
        await d.trigger("p1")

        # Ждём начала первого флаша (slow_flush инкрементит concurrent)
        await asyncio.wait_for(flush_started.wait(), timeout=0.5)

        # Шлём ещё triggers пока slow_flush спит
        await d.trigger("p1")
        await d.trigger("p1")

        # Ждём завершения всего: 0.1s (1й) + delay (0.05) + 0.1s (2й) ≈ 0.25s
        await asyncio.sleep(0.5)

        assert max_concurrent <= 1, (
            f"Параллельные флаши обнаружены! max_concurrent={max_concurrent}"
        )

    async def test_second_flush_after_first_completes(self):
        """Trigger во время on_flush → второй флаш НАЧИНАЕТСЯ после завершения первого."""
        flush_starts: list[float] = []
        flush_ends: list[float] = []
        flush_started = asyncio.Event()

        async def slow_flush(phone: str) -> None:
            flush_starts.append(asyncio.get_running_loop().time())
            flush_started.set()
            await asyncio.sleep(0.08)
            flush_ends.append(asyncio.get_running_loop().time())

        delay = 0.05
        d = Debouncer(slow_flush, delay=delay, max_wait=0.5)

        await d.trigger("p1")

        # Ждём начала первого флаша
        await asyncio.wait_for(flush_started.wait(), timeout=0.5)

        # Отправляем trigger пока slow_flush ещё выполняется
        await d.trigger("p1")

        # Ждём второго флаша: 1й(0.08) + delay(0.05) + 2й(0.08) ≈ 0.21s после старта 1го
        await asyncio.sleep(0.5)

        assert len(flush_starts) == 2, (
            f"Ожидали 2 флаша (1й + добор), получили {len(flush_starts)}"
        )
        # Второй флаш начался после конца первого (не параллельно)
        assert flush_starts[1] >= flush_ends[0], (
            f"Второй флаш начался (t={flush_starts[1]:.4f}) "
            f"до конца первого (t={flush_ends[0]:.4f})!"
        )

    async def test_re_trigger_during_flush_triggers_second_flush(self):
        """Trigger во время flush → re_trigger устанавливается → второй флаш происходит."""
        flush_count = 0
        flush_started = asyncio.Event()

        async def slow_flush(phone: str) -> None:
            nonlocal flush_count
            flush_started.set()
            await asyncio.sleep(0.08)
            flush_count += 1

        delay = 0.05
        d = Debouncer(slow_flush, delay=delay, max_wait=0.5)

        await d.trigger("p1")
        await asyncio.wait_for(flush_started.wait(), timeout=0.5)

        # Флаш в процессе — trigger ставит re_trigger=True
        await d.trigger("p1")

        # Ждём второго флаша
        await asyncio.sleep(0.5)

        assert flush_count == 2, (
            f"Ожидали 2 флаша (добор после 1го), получили {flush_count}"
        )


# ===========================================================================
# 7. Ошибка в on_flush
# ===========================================================================

class TestFlushError:
    async def test_error_in_flush_does_not_crash_debouncer(self):
        """Исключение в on_flush логируется, debouncer продолжает работать."""
        call_count = 0

        async def bad_flush(phone: str) -> None:
            nonlocal call_count
            call_count += 1
            raise ValueError(f"Специально падаем для {phone}")

        d = Debouncer(bad_flush, delay=0.05, max_wait=0.3)

        await d.trigger("bad")
        await asyncio.sleep(0.2)  # flush отработал с ошибкой

        # Debouncer не упал — call_count дошёл до 1
        assert call_count == 1

    async def test_other_phone_flushes_after_error(self):
        """После ошибки для одного номера другой номер флашится нормально."""
        good_log: list[str] = []

        async def selective_bad_flush(phone: str) -> None:
            if phone == "bad":
                raise RuntimeError("bad phone error")
            good_log.append(phone)

        d = Debouncer(selective_bad_flush, delay=0.05, max_wait=0.3)

        await d.trigger("bad")
        await asyncio.sleep(0.02)  # немного после — чтобы оба таймера независимы
        await d.trigger("good")

        await asyncio.sleep(0.3)

        assert "good" in good_log, (
            "Номер 'good' не флашнулся — ошибка 'bad' заблокировала debouncer"
        )

    async def test_retry_after_error_gives_new_flush(self):
        """После ошибки повторный trigger того же номера даёт новый флаш."""
        attempts: list[int] = []

        async def flaky_flush(phone: str) -> None:
            n = len(attempts) + 1
            attempts.append(n)
            if n == 1:
                raise RuntimeError("Первая попытка — ошибка")
            # вторая попытка — OK

        d = Debouncer(flaky_flush, delay=0.05, max_wait=0.3)

        # Первый trigger → ошибка
        await d.trigger("p1")
        await asyncio.sleep(0.2)
        assert len(attempts) == 1

        # Повторный trigger → новый флаш
        await d.trigger("p1")
        await asyncio.sleep(0.2)

        assert len(attempts) == 2, (
            f"Ожидали 2 попытки (вторая успешная), получили {len(attempts)}"
        )


# ===========================================================================
# 8. Shutdown
# ===========================================================================

class TestShutdown:
    async def test_shutdown_prevents_pending_flush(self):
        """После trigger (таймер тикает) → shutdown → флаша НЕТ."""
        log, on_flush = make_log_flush()
        # delay большой чтобы у нас было время вызвать shutdown
        d = Debouncer(on_flush, delay=0.15, max_wait=1.0)

        await d.trigger("p1")
        assert len(log) == 0

        await d.shutdown()

        # Ждём дольше delay — флаша быть не должно
        await asyncio.sleep(0.25)
        assert len(log) == 0, f"Флаш произошёл после shutdown! log={log}"

    async def test_shutdown_clears_internal_state(self):
        """После shutdown _states пустой."""
        log, on_flush = make_log_flush()
        d = Debouncer(on_flush, delay=0.1, max_wait=0.5)

        await d.trigger("p1")
        await d.trigger("p2")
        await d.trigger("p3")

        await d.shutdown()

        assert d._states == {}, f"_states не очищен после shutdown: {list(d._states.keys())}"

    async def test_shutdown_completes_without_hanging(self):
        """Shutdown завершается быстро — нет зависших тасок."""
        log, on_flush = make_log_flush()
        d = Debouncer(on_flush, delay=0.1, max_wait=0.5)

        for p in ["p1", "p2", "p3"]:
            await d.trigger(p)

        # asyncio.wait_for кидает TimeoutError если shutdown завис
        await asyncio.wait_for(d.shutdown(), timeout=2.0)
        assert d._states == {}

    async def test_double_shutdown_is_safe(self):
        """Повторный shutdown не падает (идемпотентность)."""
        log, on_flush = make_log_flush()
        d = Debouncer(on_flush, delay=0.05, max_wait=0.3)

        await d.trigger("p1")
        await d.shutdown()
        await d.shutdown()  # второй вызов — не должен выбросить исключение


class TestStaleTimerGuard:
    """Guard: устаревший таймер (не совпадает с state.timer) не должен флашить."""

    async def test_superseded_timer_does_not_flush(self):
        import asyncio
        flushes = []
        async def on_flush(phone):
            flushes.append(phone)
        d = Debouncer(on_flush, delay=0.05, max_wait=0.3)

        # смоделируем состояние, где зарегистрирован ДРУГОЙ таймер (сентинел),
        # а _timer вызывается «от имени» устаревшего — он должен выйти без флаша
        from debounce import _PhoneState
        loop = asyncio.get_running_loop()
        state = _PhoneState(first_ts=loop.time())
        sentinel = asyncio.ensure_future(asyncio.sleep(1))  # это "новый" таймер
        state.timer = sentinel
        d._states["wa_x"] = state

        # прямой вызов _timer: current_task != state.timer(sentinel) → return без флаша
        await d._timer("wa_x", state)
        assert flushes == []

        sentinel.cancel()
        try:
            await sentinel
        except asyncio.CancelledError:
            pass
        await d.shutdown()


class TestShutdownWaitsActiveFlush:
    """shutdown должен дождаться идущего on_flush (иначе close_pool порвёт запрос)."""

    async def test_shutdown_awaits_in_progress_flush(self):
        import asyncio
        done = []
        async def slow_flush(phone):
            await asyncio.sleep(0.2)   # долгая обработка (имитация AI/БД)
            done.append(phone)
        d = Debouncer(slow_flush, delay=0.05, max_wait=0.3)
        await d.trigger("wa_1")
        await asyncio.sleep(0.08)      # таймер сработал, slow_flush в процессе
        assert done == []              # ещё не завершился
        await d.shutdown()             # должен дождаться
        assert done == ["wa_1"]        # флаш доработал до конца
        assert d._states == {}
        assert d._active_flushes == set()
