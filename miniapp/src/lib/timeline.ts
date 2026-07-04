// Подписи и лейблы для единого таймлайна карточки лида.
import { stageMeta } from "./stages";
import type { FunnelStage, MessageSender, TimelineAction } from "./types";

// Подпись исходящего сообщения по тому, КАК это видит лид в WhatsApp:
// авто-ответ бота → «Бот»; ручной ответ любого человека с доступом → «Anna».
// Входящее (сам лид) — без подписи.
export function messageAuthor(sender: MessageSender): string | null {
  if (sender === "anna") return "Бот";
  if (sender === "manager") return "Anna";
  return null; // lead
}

// Текст системной строки смены стадии: «Стадия: Первичное общение → Показала цену».
export function stageChangeText(from: FunnelStage | null, to: FunnelStage): string {
  const toLabel = stageMeta(to).label;
  if (!from) return `Стадия: ${toLabel}`;
  return `Стадия: ${stageMeta(from).label} → ${toLabel}`;
}

// Текст системной строки действия менеджера.
export const ACTION_TEXT: Record<TimelineAction, string> = {
  takeover: "Взято в работу вручную",
  release: "Возвращено боту",
  stop: "Бот остановлен для лида",
  resume: "Бот снова отвечает",
  client_add: "Добавлен в список клиентов",
  client_remove: "Убран из списка клиентов",
};
