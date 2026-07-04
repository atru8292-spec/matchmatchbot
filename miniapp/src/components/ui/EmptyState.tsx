import type { ReactNode } from "react";

interface Props {
  icon: ReactNode; // Lucide-иконка
  title: string;
  description?: string;
  action?: ReactNode; // тёплый CTA (кнопка)
}

// Пустое состояние: тёплое, с иконкой и (опц.) действием — не «сухой» текст.
export function EmptyState({ icon, title, description, action }: Props) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 px-8 py-16 text-center">
      <div className="flex h-14 w-14 items-center justify-center rounded-full bg-elevated text-muted">
        {icon}
      </div>
      <div className="space-y-1">
        <h3 className="text-base text-ink">{title}</h3>
        {description && (
          <p className="mx-auto max-w-xs text-sm leading-relaxed text-muted">
            {description}
          </p>
        )}
      </div>
      {action}
    </div>
  );
}
