// Типы данных мини-CRM. Совпадают с camelCase-ответами /api/mini/* (см. mini_api.py).

export type FunnelStage =
  | "new" | "qualifying" | "photo_pending" | "qualified" | "pitched" | "videocall_set"
  | "client_agency" | "event_attended"
  | "rejected" | "lost" | "nurture";

export type LeadMode = "auto" | "manual";
export type MessageDirection = "inbound" | "outbound";
export type MessageSender = "lead" | "anna" | "manager";

export interface Lead {
  phone: string;
  name: string | null;
  whatsappName: string | null;
  funnelStage: FunnelStage;
  funnelStageLabel: string;
  mode: LeadMode;
  interest: string | null;
  age: number | null;
  profession: string | null;
  city: string | null;
  isClient: boolean;
  lastMessageAt: string | null;
  lastInboundAt: string | null;
  lastMessagePreview: string | null;
  lastMessageSender: MessageSender | null;
  lastMessageDirection: MessageDirection | null;
}

export interface LeadPhoto {
  url: string;
  verdict: "ok" | "reject" | "manual" | "payment_ok" | null;
  receivedAt: string;
}

// Действия менеджера, попадающие в таймлайн как системные строки.
export type TimelineAction =
  | "takeover" | "release" | "stop" | "resume" | "client_add" | "client_remove";

// Единый таймлайн (в духе HubSpot): сообщения + смены стадий + действия + заметки,
// слитые по времени. Тег kind различает рендер.
export type MessageStatus = "sent" | "failed" | "sending" | null;

export type TimelineItem =
  | { kind: "message"; id: string; sender: MessageSender; direction: MessageDirection; text: string; createdAt: string; status?: MessageStatus }
  | { kind: "stage"; id: string; fromStage: FunnelStage | null; toStage: FunnelStage; createdAt: string }
  | { kind: "action"; id: string; action: TimelineAction; createdAt: string }
  | { kind: "note"; id: string; text: string; createdAt: string };

// Полная карточка лида: поля списка + детали + единый таймлайн + фото.
export interface LeadDetail extends Lead {
  firstMessageAt: string | null;
  doNotContact: boolean; // «бот больше не пишет» (stop)
  clientReason: string | null; // причина, если в списке клиентов
  clientAddedBy: string | null;
  timeline: TimelineItem[];
  photos: LeadPhoto[];
}

// Клиент из списка (bot_whitelist) для экрана «Клиенты».
export interface WhitelistClient {
  phone: string;
  name: string | null;
  reason: string | null;
  addedBy: string | null;
  addedAt: string | null;
}

export interface LeadsPage {
  leads: Lead[];
  total: number;
  limit: number;
  offset: number;
  hasMore: boolean;
}

// ===== Статистика (дашборд) =====
export interface FunnelStat {
  stage: FunnelStage;
  label: string;
  total: number;
  last24h: number;
  last7d: number;
  percent: number;
}

export interface EscalationItem {
  phone: string;
  name: string | null;
  reason: string | null;
  minutesLeft: number | null;
  lastInboundAt: string | null;
}

export interface Stats {
  totalLeads: number;
  newToday: number;
  newWeek: number;
  funnel: FunnelStat[];
  pendingEscalations: { count: number; items: EscalationItem[] };
}

// ===== Настройки ивента =====
export interface EventSettings {
  eventActive: boolean;
  eventDate: string;
  eventStart: string;
  eventEnd: string;
  eventTime: string;
  eventAddress: string;
  eventPriceMember: string;
  eventPriceNonmember: string;
  eventPriceOld: string;
  eventLink: string;
  courseLink: string;
  invitationUrl: string;
  invitationReady: boolean;
  eventGuestTab: string;
}

// ===== Медиа с ивентов (галерея фото/видео) =====
export interface EventMediaItem {
  id: number;
  url: string;
  mediaType: "image" | "video";
  sizeBytes: number | null;
  isActive: boolean;
  createdAt: string | null;
}

// ===== Напоминание дня ивента (предпросмотр + ручная отправка) =====
export interface DayOfPreview {
  templateA: string[];
  templateB: string[];
}

export interface DayOfRecipient {
  phone: string;
  name: string;
  funnelStage: string;
  funnelStageLabel: string;
  template: "A" | "B";
  alreadySent: boolean;
  sentAt: string | null;
}

export interface DayOfRecipientsResponse {
  recipients: DayOfRecipient[];
  eventDate: string;
}

export interface DayOfSendResult {
  sent: { phone: string; name: string; template: string }[];
  duplicates: { phone: string; name: string; template: string; sentAt: string | null }[];
  failed: { phone: string; name: string; reason: string }[];
}

export interface LeadsQuery {
  stage?: FunnelStage[];
  mode?: LeadMode;
  search?: string;
  sort?: "recent" | "stage";
  limit?: number;
  offset?: number;
}

// ===== Тест переписки (/api/mini/test-chat) — песочница, не пишет в БД =====
export interface TestChatProfile {
  isSingle: boolean | null;
  age: number | null;
  profession: string | null;
  city: string | null;
  interest: string | null;
  photoReceived: boolean;
  funnelStage: string | null;
  whatsappName: string;
}

export interface TestRagCandidate {
  id: number;
  score: number;
  title: string | null;
}

export interface TestChatRequest {
  leadProfile: TestChatProfile;
  history: { sender: "lead" | "anna"; text: string }[];
  message: string;
}

export interface TestChatResponse {
  messages: string[];
  extracted: Record<string, unknown>;
  funnelStage: string | null;
  usedScenarioId: number | null;
  usedScenarioTitle: string | null;
  action: string;
  needsEscalation: boolean;
  ragCandidates: TestRagCandidate[];
}
