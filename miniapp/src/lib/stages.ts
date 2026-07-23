// Стадии воронки: зеркало funnel.py (коды + названия) + визуальные тона бейджей.
// Источник истины по кодам/названиям — бэкенд; здесь дублируем для оффлайн-моков
// и порядка сортировки. Тона — из brand-guide (new серый, qualified синий,
// client_* зелёный, rejected/lost приглушённый).
import type { FunnelStage } from "./types";

export type StageTone = "neutral" | "info" | "accent" | "primary" | "success" | "danger";

interface StageMeta {
  label: string;
  tone: StageTone;
}

export const STAGES: Record<FunnelStage, StageMeta> = {
  new: { label: "Новый", tone: "neutral" },
  qualifying: { label: "Первичное общение", tone: "info" },
  photo_pending: { label: "Жду фото", tone: "accent" },
  qualified: { label: "Прошёл проверку", tone: "info" },
  pitched: { label: "Показала цену", tone: "accent" },
  videocall_set: { label: "Записан на звонок", tone: "primary" },
  client_agency: { label: "Клиент агентства", tone: "success" },
  event_attended: { label: "Гость ивента", tone: "success" },
  rejected: { label: "Не подошёл", tone: "danger" },
  lost: { label: "Отказался", tone: "neutral" },
  nurture: { label: "Лист ожидания", tone: "neutral" },
};

// Порядок для фильтр-чипов и сортировки по стадии.
export const ACTIVE_STAGES: FunnelStage[] = [
  "new", "qualifying", "photo_pending", "qualified", "pitched", "videocall_set",
];

export function stageMeta(code: FunnelStage): StageMeta {
  return STAGES[code] ?? STAGES.new;
}

// Классы фон/текст для каждого тона (token-driven, см. tailwind.config + index.css).
export const TONE_CLASS: Record<StageTone, string> = {
  neutral: "bg-neutral-bg text-neutral",
  info: "bg-info-bg text-info",
  accent: "bg-accent-bg text-accent-ink",
  primary: "bg-primary/10 text-primary",
  success: "bg-success-bg text-success",
  danger: "bg-danger-bg text-danger",
};
