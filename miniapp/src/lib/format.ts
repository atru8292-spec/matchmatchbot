// Форматирование для UI: относительное время, телефон, инициалы.

/** Относительное время «5 мин / 3 ч / 2 дн», иначе короткая дата. Русский. */
export function relativeTime(iso: string | null, now: Date = new Date()): string {
  if (!iso) return "";
  const then = new Date(iso);
  const diffSec = Math.floor((now.getTime() - then.getTime()) / 1000);
  if (diffSec < 60) return "только что";
  const min = Math.floor(diffSec / 60);
  if (min < 60) return `${min} мин`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr} ч`;
  const days = Math.floor(hr / 24);
  if (days < 7) return `${days} дн`;
  return then.toLocaleDateString("ru-RU", { day: "numeric", month: "short" });
}

/** 'wa_521234567890' / '+52...' → '+52 123 456 7890' (косметика, группами). */
export function formatPhone(phone: string | null): string {
  if (!phone) return "";
  const digits = phone.replace(/\D/g, "");
  if (!digits) return phone;
  // группируем хвост по 3-3-4, префикс страны отдельно
  const tail = digits.slice(-10);
  const cc = digits.slice(0, -10);
  const grouped = tail.replace(/(\d{3})(\d{3})(\d{0,4})/, "$1 $2 $3").trim();
  return `+${cc}${cc ? " " : ""}${grouped}`.trim();
}

/** Инициалы из имени: «Maria Lopez» → «ML», иначе первая буква телефона. */
export function initials(name: string | null, phone: string): string {
  const src = (name || "").trim();
  if (src) {
    const parts = src.split(/\s+/).slice(0, 2);
    return parts.map((p) => p[0]?.toUpperCase() ?? "").join("");
  }
  const digits = phone.replace(/\D/g, "");
  return digits.slice(-2, -1) || "•";
}

/** Время сообщения «14:05» для чата. */
export function formatClock(iso: string): string {
  return new Date(iso).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });
}

/** Дата-разделитель в чате: «Сегодня» / «Вчера» / «1 июля». */
export function formatDay(iso: string, now: Date = new Date()): string {
  const d = new Date(iso);
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const days = Math.round((startOf(now) - startOf(d)) / 86_400_000);
  if (days === 0) return "Сегодня";
  if (days === 1) return "Вчера";
  return d.toLocaleDateString("ru-RU", { day: "numeric", month: "long" });
}

/** Кто отправил последнее сообщение — короткий префикс для превью. */
export function senderPrefix(sender: string | null): string {
  if (sender === "anna") return "Anna: ";
  if (sender === "manager") return "Вы: ";
  return "";
}
