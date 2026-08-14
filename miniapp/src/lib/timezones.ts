// Список IANA часовых поясов для выпадающего списка (расписание звонков, AssigneesScreen).
// Intl.supportedValuesOf доступен в современных движках (Chrome/WebView 99+, Safari 17+);
// если Telegram открывает более старый WebView — fallback на курированный список (покрывает
// реальные случаи: Мексика/Латам, Европа, Россия/СНГ, США).
const FALLBACK_TIMEZONES = [
  "America/Mexico_City", "America/Bogota", "America/Lima", "America/Santiago",
  "America/Argentina/Buenos_Aires", "America/New_York", "America/Chicago",
  "America/Denver", "America/Los_Angeles",
  "Europe/Madrid", "Europe/London", "Europe/Berlin", "Europe/Paris", "Europe/Warsaw",
  "Europe/Kyiv", "Europe/Minsk", "Europe/Moscow", "Europe/Lisbon",
  "Asia/Dubai", "Asia/Almaty",
];

export function listTimezones(): string[] {
  try {
    const supported = (Intl as unknown as { supportedValuesOf?: (k: string) => string[] })
      .supportedValuesOf?.("timeZone");
    if (supported && supported.length) return supported;
  } catch {
    // Intl.supportedValuesOf no disponible — fallback abajo
  }
  return FALLBACK_TIMEZONES;
}
