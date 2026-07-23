// HTTP-клиент мини-CRM. Добавляет заголовок Authorization: tma <initData>
// (если открыто внутри Telegram) и разбирает ответы /api/mini/*.
//
// Пока USE_MOCKS=true (Фаза 0) реальные запросы к бэкенду не идут — данные из mock.ts.
// При соединении с API просто выключим USE_MOCKS и включим fetchLeadsApi.
import { initTelegram } from "./telegram";
import {
  mockClients, mockGetEvent, mockLeadDetail, mockLeads, mockLeadsCsv, mockSetEvent, mockStats, USE_MOCKS,
} from "./mock";
import type {
  DayOfPreview, DayOfRecipientsResponse, DayOfSendResult,
  EventMediaItem, EventSettings, LeadDetail, LeadsPage, LeadsQuery, Stats,
  TestChatRequest, TestChatResponse, TimelineItem, WhitelistClient,
} from "./types";

const BASE = (import.meta as any).env?.VITE_API_BASE ?? "/api/mini";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function apiFetch<T>(path: string, params?: Record<string, unknown>): Promise<T> {
  const { initDataRaw } = initTelegram();
  const url = new URL(BASE + path, window.location.origin);
  for (const [k, v] of Object.entries(params ?? {})) {
    if (v == null) continue;
    if (Array.isArray(v)) v.forEach((item) => url.searchParams.append(k, String(item)));
    else url.searchParams.set(k, String(v));
  }
  const headers: Record<string, string> = {};
  if (initDataRaw) headers["Authorization"] = `tma ${initDataRaw}`;

  const res = await fetch(url.toString(), { headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json())?.detail ?? detail;
    } catch { /* тело не JSON — оставляем statusText */ }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

// Мутация (POST/DELETE) с JSON-телом. Заголовок авторизации — как в apiFetch.
async function apiSend<T>(path: string, method: "POST" | "PUT" | "DELETE", body?: unknown): Promise<T> {
  const { initDataRaw } = initTelegram();
  const url = new URL(BASE + path, window.location.origin);
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (initDataRaw) headers["Authorization"] = `tma ${initDataRaw}`;

  const res = await fetch(url.toString(), {
    method, headers, body: body != null ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json())?.detail ?? detail;
    } catch { /* не JSON */ }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

export async function fetchLeads(q: LeadsQuery): Promise<LeadsPage> {
  if (USE_MOCKS) {
    // небольшая задержка — увидеть скелетоны в dev
    await new Promise((r) => setTimeout(r, 250));
    return mockLeads(q);
  }
  return apiFetch<LeadsPage>("/leads", {
    stage: q.stage, mode: q.mode, search: q.search, sort: q.sort,
    limit: q.limit, offset: q.offset,
  });
}

// Ручная отправка сообщения лиду (от имени Anna). Возврат — созданное сообщение
// (kind=message, status sent|failed) + delivered/tookOver.
export interface SendResult {
  message: TimelineItem;
  delivered: boolean;
  tookOver: boolean;
}
// override=true — Аня подтвердила отправку лиду с do_not_contact (opt-out). Без него
// бэкенд отдаёт 409 (ApiError), UI показывает предупреждение и переспрашивает.
export async function sendMessage(phone: string, text: string, override = false): Promise<SendResult> {
  if (USE_MOCKS) {
    await new Promise((r) => setTimeout(r, 400));
    return {
      message: {
        kind: "message", id: `msg-mock-${text.length}`, sender: "manager",
        direction: "outbound", text, createdAt: new Date().toISOString(), status: "sent",
      },
      delivered: true, tookOver: false,
    };
  }
  return apiSend<SendResult>(`/lead/${enc(phone)}/message`, "POST", { text, override });
}

export async function fetchLeadDetail(phone: string): Promise<LeadDetail> {
  if (USE_MOCKS) {
    await new Promise((r) => setTimeout(r, 200));
    const detail = mockLeadDetail(phone);
    if (!detail) throw new ApiError(404, "Лид не найден");
    return detail;
  }
  // Факты и таймлайн — раздельные эндпоинты (/lead + /history), собираем в LeadDetail.
  const p = encodeURIComponent(phone);
  const [facts, history] = await Promise.all([
    apiFetch<Omit<LeadDetail, "timeline">>(`/lead/${p}`),
    apiFetch<{ timeline: LeadDetail["timeline"] }>(`/lead/${p}/history`),
  ]);
  return { ...facts, timeline: history.timeline };
}

// ===== Действия с карточки (мутации). В USE_MOCKS — no-op resolve (оффлайн-дев). =====
const enc = (phone: string) => encodeURIComponent(phone);

export const leadActions = {
  takeover: (phone: string) => post(`/lead/${enc(phone)}/takeover`),
  release: (phone: string) => post(`/lead/${enc(phone)}/release`),
  stop: (phone: string) => post(`/lead/${enc(phone)}/stop`),
  resume: (phone: string) => post(`/lead/${enc(phone)}/resume`),
  addNote: (phone: string, text: string) => post(`/lead/${enc(phone)}/notes`, { text }),
  addWhitelist: (phone: string, reason?: string) => post(`/lead/${enc(phone)}/whitelist`, { reason }),
  removeWhitelist: (phone: string) => del(`/lead/${enc(phone)}/whitelist`),
};

function post(path: string, body?: unknown) {
  if (USE_MOCKS) return Promise.resolve({ ok: true });
  return apiSend(path, "POST", body);
}
function del(path: string) {
  if (USE_MOCKS) return Promise.resolve({ ok: true });
  return apiSend(path, "DELETE");
}

// ===== Экран «Клиенты» (whitelist) =====
export async function fetchClients(): Promise<WhitelistClient[]> {
  if (USE_MOCKS) {
    await new Promise((r) => setTimeout(r, 200));
    return mockClients();
  }
  return (await apiFetch<{ clients: WhitelistClient[] }>("/whitelist")).clients;
}

export const clientActions = {
  add: (phone: string, reason?: string) => post("/whitelist", { phone, reason }),
  remove: (phone: string) => del(`/whitelist/${enc(phone)}`),
};

// ===== Статистика =====
export async function fetchStats(): Promise<Stats> {
  if (USE_MOCKS) {
    await new Promise((r) => setTimeout(r, 200));
    return mockStats();
  }
  return apiFetch<Stats>("/stats");
}

// ===== Настройки ивента =====
export async function fetchEvent(): Promise<EventSettings> {
  if (USE_MOCKS) {
    await new Promise((r) => setTimeout(r, 150));
    return mockGetEvent();
  }
  return apiFetch<EventSettings>("/event");
}

export async function saveEvent(s: EventSettings): Promise<EventSettings> {
  if (USE_MOCKS) {
    await new Promise((r) => setTimeout(r, 300));
    return mockSetEvent(s);
  }
  return apiSend<EventSettings>("/event", "PUT", s);
}

// ===== Глобальная пауза бота (тех. режим) =====
export async function fetchBotPaused(): Promise<boolean> {
  if (USE_MOCKS) return false;
  const m = await apiFetch<{ botPaused: boolean }>("/meta");
  return !!m.botPaused;
}

export async function setBotPause(paused: boolean): Promise<{ botPaused: boolean }> {
  if (USE_MOCKS) return { botPaused: paused };
  return apiSend<{ botPaused: boolean }>("/bot/pause", "POST", { paused });
}

// ===== Напоминание дня ивента =====
export async function fetchDayOfPreview(): Promise<DayOfPreview> {
  return apiFetch<DayOfPreview>("/event/day-of/preview");
}

export async function fetchDayOfRecipients(): Promise<DayOfRecipientsResponse> {
  return apiFetch<DayOfRecipientsResponse>("/event/day-of/recipients");
}

export async function sendDayOf(
  recipients: { phone: string; template: string }[], force: boolean,
): Promise<DayOfSendResult> {
  return apiSend<DayOfSendResult>("/event/day-of/send", "POST", { recipients, force });
}

// Файл → base64 (без префикса data:...;base64,).
function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const r = new FileReader();
    r.onload = () => {
      const s = String(r.result);
      resolve(s.slice(s.indexOf(",") + 1));
    };
    r.onerror = () => reject(new Error("Не удалось прочитать файл"));
    r.readAsDataURL(file);
  });
}

// Загрузить файл картинки-приглашения → Storage → вернуть public URL.
// ===== Медиа с ивентов (галерея фото/видео, бот шлёт лиду) =====
export async function fetchEventMedia(): Promise<EventMediaItem[]> {
  if (USE_MOCKS) return [];
  return (await apiFetch<{ media: EventMediaItem[] }>("/event/media")).media;
}

// Загрузка multipart (видео сжимается на сервере; при отказе — 422 с текстом от бэка).
export async function uploadEventMedia(file: File): Promise<EventMediaItem> {
  const { initDataRaw } = initTelegram();
  const url = new URL(BASE + "/event/media", window.location.origin);
  const form = new FormData();
  form.append("file", file);
  const headers: Record<string, string> = {};
  if (initDataRaw) headers["Authorization"] = `tma ${initDataRaw}`;
  const res = await fetch(url.toString(), { method: "POST", headers, body: form });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json())?.detail ?? detail; } catch { /* not json */ }
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<EventMediaItem>;
}

export async function deleteEventMedia(id: number): Promise<void> {
  await apiSend(`/event/media/${id}`, "DELETE");
}

export async function uploadInvitation(file: File): Promise<string> {
  if (USE_MOCKS) {
    await new Promise((r) => setTimeout(r, 400));
    return URL.createObjectURL(file); // локальное превью для оффлайн-дев
  }
  const contentBase64 = await fileToBase64(file);
  const res = await apiSend<{ url: string }>("/event/invitation", "POST", {
    contentBase64, contentType: file.type,
  });
  return res.url;
}

// ===== Экспорт лидов в CSV =====
function triggerDownload(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// Скачать CSV текущей отфильтрованной выборки (те же фильтры, что на экране «Лиды»).
// Файл собирает бэкенд (весь набор, не только страница). В USE_MOCKS — из моков.
export async function downloadLeadsExport(q: LeadsQuery): Promise<void> {
  if (USE_MOCKS) {
    triggerDownload(new Blob(["﻿" + mockLeadsCsv(q)], { type: "text/csv;charset=utf-8" }),
      "matchmatch-leads.csv");
    return;
  }
  const { initDataRaw } = initTelegram();
  const url = new URL(BASE + "/leads/export", window.location.origin);
  const params: Record<string, unknown> = { stage: q.stage, mode: q.mode, search: q.search, sort: q.sort };
  for (const [k, v] of Object.entries(params)) {
    if (v == null) continue;
    if (Array.isArray(v)) v.forEach((i) => url.searchParams.append(k, String(i)));
    else url.searchParams.set(k, String(v));
  }
  const headers: Record<string, string> = {};
  if (initDataRaw) headers["Authorization"] = `tma ${initDataRaw}`;

  const res = await fetch(url.toString(), { headers });
  if (!res.ok) throw new ApiError(res.status, res.statusText);
  triggerDownload(await res.blob(), "matchmatch-leads.csv");
}

// Тест переписки: прогнать сообщение через реальный пайплайн бота (read-only, без записи).
export async function sendTestChat(body: TestChatRequest): Promise<TestChatResponse> {
  return apiSend<TestChatResponse>("/test-chat", "POST", body);
}
