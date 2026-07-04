// Моковые лиды для локальной разработки без бэкенда.
// ВАЖНО (бизнес-логика, см. CLAUDE.md): лиды бота Anna = ТОЛЬКО МУЖЧИНЫ-мексиканцы,
// ищущие женщину через агентство. Испанский, мужские имена/профессии, обращение «guapo»,
// «eres soltero». Славянки — это НЕ лиды (женщины из базы агентства, отдельная история).
// USE_MOCKS=false — данные идут с реального /api/mini/*; моки только для оффлайн-разработки.
import { STAGES, stageMeta } from "./stages";
import type {
  EventSettings, FunnelStage, Lead, LeadDetail, LeadsPage, LeadsQuery, Stats, TimelineItem, WhitelistClient,
} from "./types";

export const USE_MOCKS = false;

const now = Date.now();
const ago = (min: number) => new Date(now - min * 60_000).toISOString();

const LEADS: Lead[] = [
  {
    phone: "wa_5215512345678", name: "Carlos Mendoza", whatsappName: "Carlos",
    funnelStage: "pitched", funnelStageLabel: "Показала цену", mode: "auto",
    interest: "agency", age: 42, profession: "Abogado", city: "CDMX",
    isClient: false, lastMessageAt: ago(4), lastInboundAt: ago(4),
    lastMessagePreview: "¿Y eso incluye las presentaciones en persona?",
    lastMessageSender: "lead", lastMessageDirection: "inbound",
  },
  {
    phone: "wa_5218187654321", name: "Miguel Ángel Torres", whatsappName: "Miguel",
    funnelStage: "videocall_set", funnelStageLabel: "Записан на звонок", mode: "manual",
    interest: "agency", age: 47, profession: "Empresario", city: "Monterrey",
    isClient: false, lastMessageAt: ago(52), lastInboundAt: ago(90),
    lastMessagePreview: "Perfecto, nos vemos el jueves a las 6",
    lastMessageSender: "manager", lastMessageDirection: "outbound",
  },
  {
    phone: "wa_5215599887766", name: "Fernando Lozano", whatsappName: "Fer",
    funnelStage: "photo_pending", funnelStageLabel: "Жду фото", mode: "auto",
    interest: "both", age: 44, profession: "Ingeniero", city: "CDMX",
    isClient: false, lastMessageAt: ago(180), lastInboundAt: ago(180),
    lastMessagePreview: "Ahorita te mando una foto guapa 📸",
    lastMessageSender: "lead", lastMessageDirection: "inbound",
  },
  {
    phone: "wa_5213322110099", name: "Ricardo Salinas", whatsappName: "Ricardo",
    funnelStage: "qualifying", funnelStageLabel: "Первичное общение", mode: "auto",
    interest: "event", age: 39, profession: "Director financiero", city: "Guadalajara",
    isClient: false, lastMessageAt: ago(320), lastInboundAt: ago(320),
    lastMessagePreview: "Hola, vi su página, quiero información",
    lastMessageSender: "lead", lastMessageDirection: "inbound",
  },
  {
    phone: "wa_5215544332211", name: "Alejandro Ríos", whatsappName: "Alex",
    funnelStage: "client_starter", funnelStageLabel: "Клиент Starter", mode: "manual",
    interest: "agency", age: 50, profession: "Empresario", city: "CDMX",
    isClient: true, lastMessageAt: ago(1450), lastInboundAt: ago(1600),
    lastMessagePreview: "Gracias Anna, quedo atento 🙏",
    lastMessageSender: "lead", lastMessageDirection: "inbound",
  },
  {
    phone: "wa_5216677889900", name: null, whatsappName: "Roberto",
    funnelStage: "new", funnelStageLabel: "Новый", mode: "auto",
    interest: null, age: null, profession: null, city: null,
    isClient: false, lastMessageAt: ago(12), lastInboundAt: ago(12),
    lastMessagePreview: "Buenas, información por favor",
    lastMessageSender: "lead", lastMessageDirection: "inbound",
  },
  {
    phone: "wa_5215500112233", name: "Diego Herrera", whatsappName: "Diego",
    funnelStage: "qualified", funnelStageLabel: "Прошёл проверку", mode: "auto",
    interest: "agency", age: 38, profession: "Médico cirujano", city: "Puebla",
    isClient: false, lastMessageAt: ago(2600), lastInboundAt: ago(2600),
    lastMessagePreview: "Me interesa mucho, ¿cómo seguimos?",
    lastMessageSender: "lead", lastMessageDirection: "inbound",
  },
  {
    phone: "wa_5218811223344", name: "Javier Núñez", whatsappName: "Javier",
    funnelStage: "lost", funnelStageLabel: "Отказался", mode: "auto",
    interest: "agency", age: 52, profession: "Arquitecto", city: "Mérida",
    isClient: false, lastMessageAt: ago(5000), lastInboundAt: ago(5200),
    lastMessagePreview: "Lo voy a pensar, gracias",
    lastMessageSender: "lead", lastMessageDirection: "inbound",
  },
];

// Моковые таймлайны (единый: сообщения + смены стадий + действия + заметки).
// Диалог — реальная квалификация мужчины-лида по сценариям (eres soltero → возраст/
// профессия → фото → цена $1,400 на eslavas). Ключ — телефон лида.
const TIMELINES: Record<string, TimelineItem[]> = {
  wa_5215512345678: [
    { kind: "message", id: "m1", direction: "inbound", sender: "lead", text: "Hola, vi su página de MatchMatch", createdAt: ago(320) },
    { kind: "stage", id: "s1", fromStage: "new", toStage: "qualifying", createdAt: ago(319) },
    { kind: "message", id: "m2", direction: "outbound", sender: "anna", text: "Hola! Soy Anna, fundadora de MatchMatch 🤍 Antes de contarte, eres soltero?", createdAt: ago(316) },
    { kind: "message", id: "m3", direction: "inbound", sender: "lead", text: "Sí, soltero", createdAt: ago(306) },
    { kind: "message", id: "m4", direction: "outbound", sender: "anna", text: "Súper. Y cuántos años tienes y a qué te dedicas? 😊", createdAt: ago(305) },
    { kind: "message", id: "m5", direction: "inbound", sender: "lead", text: "Tengo 42 años, soy abogado, tengo mi despacho en CDMX", createdAt: ago(300) },
    { kind: "note", id: "n1", text: "Профиль ок, профессия престижная, холост. Двигаем к цене.", createdAt: ago(200) },
    { kind: "message", id: "m6", direction: "outbound", sender: "anna", text: "Muchas gracias guapo 🤍 Soy tu matchmaker personal: cada mes te presento 3 mujeres eslavas seleccionadas para ti. El acompañamiento es de $1,400 USD al mes.", createdAt: ago(122) },
    { kind: "stage", id: "s2", fromStage: "qualifying", toStage: "pitched", createdAt: ago(121) },
    { kind: "message", id: "m7", direction: "inbound", sender: "lead", text: "¿Y eso incluye las presentaciones en persona?", createdAt: ago(4) },
  ],
  wa_5218187654321: [
    { kind: "message", id: "m1", direction: "inbound", sender: "lead", text: "Buenas, me interesa conocer mujeres rusas", createdAt: ago(200) },
    { kind: "message", id: "m2", direction: "outbound", sender: "anna", text: "Hola! Qué bueno que te interesa 🤍 soy Anna de MatchMatch. Antes, eres soltero?", createdAt: ago(196) },
    { kind: "action", id: "a1", action: "takeover", createdAt: ago(170) },
    { kind: "message", id: "m3", direction: "outbound", sender: "manager", text: "Hola Miguel, claro que sí. ¿Te late si lo vemos en una videollamada rápida?", createdAt: ago(165) },
    { kind: "message", id: "m4", direction: "inbound", sender: "lead", text: "Sí, perfecto", createdAt: ago(90) },
    { kind: "stage", id: "s1", fromStage: "qualified", toStage: "videocall_set", createdAt: ago(80) },
    { kind: "message", id: "m5", direction: "outbound", sender: "manager", text: "Perfecto, nos vemos el jueves a las 6 🤍", createdAt: ago(52) },
  ],
};

function detailFor(lead: Lead): LeadDetail {
  return {
    ...lead,
    firstMessageAt: ago(400),
    doNotContact: false,
    clientReason: lead.isClient ? "Клиент агентства" : null,
    clientAddedBy: lead.isClient ? "@arinashrr" : null,
    timeline: TIMELINES[lead.phone] ?? [
      { kind: "message", id: "m1", direction: "inbound", sender: "lead", text: lead.lastMessagePreview ?? "—", createdAt: lead.lastMessageAt ?? ago(10) },
    ],
    photos: [],
  };
}

export function mockLeadDetail(phone: string): LeadDetail | null {
  const lead = LEADS.find((l) => l.phone === phone);
  return lead ? detailFor(lead) : null;
}

const INTEREST_CSV: Record<string, string> = {
  event: "Ивент", agency: "Агентство", both: "Ивент+агентство",
};

// CSV из моков (оффлайн-дев) — те же колонки, что бэкенд. ';' + минимальное экранирование.
export function mockLeadsCsv(q: LeadsQuery): string {
  const cell = (v: unknown) => {
    const s = v == null ? "" : String(v);
    return /[;"\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const headers = ["Имя", "Телефон", "Стадия", "Интерес", "Возраст", "Профессия",
    "Город", "Клиент", "Последнее сообщение", "Дата последнего сообщения"];
  const rows = mockLeads({ ...q, limit: 10000 }).leads.map((l) => [
    l.name || l.whatsappName || "",
    "+" + l.phone.replace(/\D/g, ""),
    l.funnelStageLabel,
    l.interest ? INTEREST_CSV[l.interest] ?? l.interest : "",
    l.age ?? "",
    l.profession ?? "",
    l.city ?? "",
    l.isClient ? "Да" : "",
    l.lastMessagePreview ?? "",
    l.lastMessageAt ? l.lastMessageAt.slice(0, 16).replace("T", " ") : "",
  ].map(cell).join(";"));
  return [headers.join(";"), ...rows].join("\r\n");
}

// Моковые настройки ивента (изменяемый стор — saveEvent обновляет).
let mockEventStore: EventSettings = {
  eventActive: true, eventDate: "2026-08-15", eventTime: "20:30",
  eventAddress: "Av. Reforma 123, CDMX", eventLink: "https://matchmatchagency.com/evento",
  courseLink: "https://matchmatchagency.com/cursos",
  invitationUrl: "", invitationReady: false,
};
export function mockGetEvent(): EventSettings { return { ...mockEventStore }; }
export function mockSetEvent(s: EventSettings): EventSettings {
  mockEventStore = { ...s };
  return { ...mockEventStore };
}

// Моковая статистика — агрегируем LEADS по стадиям + фейковая эскалация.
export function mockStats(): Stats {
  const total = LEADS.length;
  const counts: Partial<Record<FunnelStage, number>> = {};
  LEADS.forEach((l) => { counts[l.funnelStage] = (counts[l.funnelStage] ?? 0) + 1; });
  const funnel = (Object.keys(STAGES) as FunnelStage[])
    .filter((code) => counts[code])
    .map((code) => ({
      stage: code, label: stageMeta(code).label, total: counts[code]!,
      last24h: counts[code]!, last7d: counts[code]!,
      percent: total ? Math.round((counts[code]! * 100) / total) : 0,
    }));
  return {
    totalLeads: total, newToday: 2, newWeek: total, funnel,
    pendingEscalations: {
      count: 1,
      items: [{
        phone: "wa_5213322110099", name: "Ricardo",
        reason: "VIP: menciona presupuesto alto", minutesLeft: 34, lastInboundAt: ago(45),
      }],
    },
  };
}

// Моковые клиенты (whitelist) — мужчины, бот для них молчит, ведёт Аня лично.
export function mockClients(): WhitelistClient[] {
  return [
    { phone: "wa_5215544332211", name: "Alejandro Ríos", reason: "Клиент агентства", addedBy: "@arinashrr", addedAt: ago(1450) },
    { phone: "wa_5215500998877", name: "Eduardo Vega", reason: "VIP, ведёт Аня", addedBy: "@arinashrr", addedAt: ago(4300) },
    { phone: "wa_5218100112244", name: null, reason: null, addedBy: "@dev", addedAt: ago(9000) },
  ];
}

/** Локальная фильтрация/поиск моков — имитирует поведение бэкенда для каркаса. */
export function mockLeads(q: LeadsQuery): LeadsPage {
  let rows = [...LEADS];
  if (q.stage?.length) rows = rows.filter((l) => q.stage!.includes(l.funnelStage));
  if (q.mode) rows = rows.filter((l) => l.mode === q.mode);
  if (q.search?.trim()) {
    const s = q.search.trim().toLowerCase();
    rows = rows.filter(
      (l) =>
        (l.name || "").toLowerCase().includes(s) ||
        (l.whatsappName || "").toLowerCase().includes(s) ||
        l.phone.includes(s.replace(/\D/g, "")),
    );
  }
  rows.sort((a, b) => (b.lastMessageAt || "").localeCompare(a.lastMessageAt || ""));
  const total = rows.length;
  const offset = q.offset ?? 0;
  const limit = q.limit ?? 20;
  const page = rows.slice(offset, offset + limit);
  return { leads: page, total, limit, offset, hasMore: offset + page.length < total };
}
