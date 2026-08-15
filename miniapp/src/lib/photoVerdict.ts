// Вердикт Vision → короткая подпись + тон (бейдж на превью фото). Общий для
// PhotoGallery (вкладка Профиль) и PhotoMessageBubble (фото в ленте Истории).
export const VERDICT: Record<string, { label: string; cls: string }> = {
  ok: { label: "Проверено", cls: "bg-success-bg text-success" },
  payment_ok: { label: "Оплата ок", cls: "bg-success-bg text-success" },
  reject: { label: "Отклонено", cls: "bg-danger-bg text-danger" },
  manual: { label: "На проверке", cls: "bg-accent-bg text-accent-ink" },
};
