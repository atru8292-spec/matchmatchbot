#!/usr/bin/env python3
"""Считает расход OpenAI (gpt-4.1) из логов бота: за день, за неделю, в среднем на лида.

Источник — строки, которые бот уже пишет сам:
  • "OpenAI usage: prompt=… (cached=…) completion=… total=…"  (ai.py) → стоимость
  • "отправлено N/M лиду <phone>"                              (sender.py) → уникальные лиды

Считается ТОЛЬКО gpt-4.1 chat (это ~99% расхода). Эмбеддинги (text-embedding-3-small)
и фото-модерация (gpt-4o-mini) в usage-лог не пишутся и здесь не учитываются — они
пренебрежимо малы (доли процента).

Запуск на сервере (лог за неделю, чтобы хватило и на день, и на неделю):
    journalctl -u matchmatch-bot -o short-iso --since "7 days ago" | python3 scripts/openai_cost.py

Или из готового файла логов:
    python3 scripts/openai_cost.py < mylog.txt
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# ── Ценник gpt-4.1 (USD за 1M токенов), актуально 2026-07 ──────────────────────
PRICE_INPUT = 2.00          # обычный prompt-токен
PRICE_CACHED = 0.50         # закэшированный prompt-токен (в 4 раза дешевле)
PRICE_OUTPUT = 8.00         # completion-токен
PER_M = 1_000_000

# prompt в usage ВКЛЮЧАЕТ cached; поэтому uncached = prompt - cached.
_USAGE_RE = re.compile(
    r"OpenAI usage:\s*prompt=(\d+)\s*\(cached=(None|\d+)\)\s*"
    r"completion=(\d+)\s*total=(\d+)"
)
# "отправлено 3/3 лиду 5215512345678" — %s это phone/chat_id
_SENT_RE = re.compile(r"отправлено\s+\d+/\d+\s+лиду\s+(\S+)")
# Ведущий timestamp journalctl -o short-iso: 2026-07-06T07:30:55+0000
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})([+-]\d{4})?")


def _cost(prompt: int, cached: int, completion: int) -> float:
    uncached = max(prompt - cached, 0)
    return (uncached * PRICE_INPUT + cached * PRICE_CACHED
            + completion * PRICE_OUTPUT) / PER_M


def _parse_date(line: str) -> str | None:
    """Дата YYYY-MM-DD из ведущего ISO-таймстампа строки, иначе None."""
    m = _TS_RE.match(line)
    return m.group(1) if m else None


def main() -> None:
    # по дате: стоимость, число вызовов, множество телефонов, число ответов
    cost_by_day: dict[str, float] = defaultdict(float)
    calls_by_day: dict[str, int] = defaultdict(int)
    leads_by_day: dict[str, set[str]] = defaultdict(set)
    replies_by_day: dict[str, int] = defaultdict(int)
    tok_in_by_day: dict[str, int] = defaultdict(int)
    tok_out_by_day: dict[str, int] = defaultdict(int)
    no_date = 0

    for line in sys.stdin:
        day = _parse_date(line)

        mu = _USAGE_RE.search(line)
        if mu:
            prompt = int(mu.group(1))
            cached = 0 if mu.group(2) == "None" else int(mu.group(2))
            completion = int(mu.group(3))
            key = day or "без-даты"
            if not day:
                no_date += 1
            cost_by_day[key] += _cost(prompt, cached, completion)
            calls_by_day[key] += 1
            tok_in_by_day[key] += prompt
            tok_out_by_day[key] += completion
            continue

        ms = _SENT_RE.search(line)
        if ms and day:
            leads_by_day[day].add(ms.group(1))
            replies_by_day[day] += 1

    all_days = sorted(d for d in cost_by_day if d not in ("без-даты",))
    if not all_days and "без-даты" not in cost_by_day:
        print("Не нашёл ни одной строки 'OpenAI usage' в логах.\n"
              "Проверь: journalctl -u matchmatch-bot -o short-iso --since '7 days ago' | "
              "python3 scripts/openai_cost.py", file=sys.stderr)
        sys.exit(1)

    # ── По дням ───────────────────────────────────────────────────────────────
    print("=" * 72)
    print("РАСХОД OpenAI (gpt-4.1) ПО ДНЯМ")
    print("=" * 72)
    print(f"{'дата':<12}{'вызовов':>9}{'лидов':>8}{'$ всего':>11}"
          f"{'$/вызов':>10}{'$/лид':>9}")
    print("-" * 72)
    for d in all_days:
        calls = calls_by_day[d]
        leads = len(leads_by_day.get(d, ()))
        cost = cost_by_day[d]
        per_call = cost / calls if calls else 0
        per_lead = cost / leads if leads else 0
        print(f"{d:<12}{calls:>9}{leads:>8}{cost:>11.4f}"
              f"{per_call:>10.5f}{per_lead:>9.4f}")

    # ── Сегодня / последние 7 дней ──────────────────────────────────────────────
    today = max(all_days) if all_days else None  # «сегодня» = последний день в логе
    week_cut = None
    if today:
        td = datetime.strptime(today, "%Y-%m-%d").date()
        week_cut = (td - timedelta(days=6)).isoformat()

    def _bucket(days: list[str]) -> tuple[float, int, int, int, int]:
        c = sum(cost_by_day[d] for d in days)
        calls = sum(calls_by_day[d] for d in days)
        leads = len(set().union(*(leads_by_day.get(d, set()) for d in days))) if days else 0
        ti = sum(tok_in_by_day[d] for d in days)
        to = sum(tok_out_by_day[d] for d in days)
        return c, calls, leads, ti, to

    def _report(title: str, days: list[str]) -> None:
        cost, calls, leads, ti, to = _bucket(days)
        print(f"\n{title}")
        print(f"  вызовов gpt-4.1 : {calls}")
        print(f"  уникальных лидов: {leads}")
        print(f"  токенов in/out  : {ti:,} / {to:,}")
        print(f"  стоимость       : ${cost:.4f}")
        if calls:
            print(f"  в среднем/вызов : ${cost / calls:.5f}")
        if leads:
            print(f"  в среднем/лид   : ${cost / leads:.4f}")

    print("\n" + "=" * 72)
    print("ИТОГИ")
    print("=" * 72)
    if today:
        _report(f"ЗА ДЕНЬ ({today})", [today])
    if week_cut:
        week_days = [d for d in all_days if d >= week_cut]
        _report(f"ЗА НЕДЕЛЮ ({week_cut} … {today})", week_days)

    if no_date:
        print(f"\n⚠ {no_date} usage-строк без таймстампа (учтены в 'без-даты', не в днях). "
              f"Запусти journalctl с '-o short-iso'.", file=sys.stderr)


if __name__ == "__main__":
    main()
