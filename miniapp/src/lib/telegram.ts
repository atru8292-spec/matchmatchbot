// Интеграция с Telegram Mini App SDK (@telegram-apps/sdk v3).
//
// Нюанс dev-режима: вне Telegram (локальный Vite) SDK не может получить launch
// params и init()/retrieveRawInitData() бросают. Мы это перехватываем и работаем
// как isTelegram=false, initDataRaw=null — фронт уходит в dev-ветку (моки/локальный
// бэкенд с mini_dev_mode). На проде внутри Telegram initDataRaw заполнен и уходит
// в заголовок Authorization: tma <initData> (см. api.ts, mini_auth.py).
import { init, retrieveRawInitData } from "@telegram-apps/sdk";

export interface TgContext {
  isTelegram: boolean;
  initDataRaw: string | null;
}

let cached: TgContext | null = null;

export function initTelegram(): TgContext {
  if (cached) return cached;
  try {
    init(); // принудительная инициализация — без неё retrieve* не работает
    const raw = retrieveRawInitData();
    cached = { isTelegram: Boolean(raw), initDataRaw: raw ?? null };
  } catch {
    // Вне Telegram (локальная разработка) — тихо уходим в dev-режим.
    cached = { isTelegram: false, initDataRaw: null };
  }
  return cached;
}

/** Синхронизировать тему: Telegram colorScheme → класс .dark на <html>. */
export function applyColorScheme(): void {
  const tg = (window as any)?.Telegram?.WebApp;
  const scheme: string | undefined = tg?.colorScheme;
  const prefersDark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
  const dark = scheme ? scheme === "dark" : Boolean(prefersDark);
  document.documentElement.classList.toggle("dark", dark);
}
