import type { ReactNode } from "react";
import { formatClock } from "@/lib/format";

// Системная строка таймлайна (смена стадии / действие менеджера).
// Центрированная, приглушённая — не конкурирует с чат-пузырями, но видна в потоке.
export function SystemRow({ icon, text, createdAt }: { icon: ReactNode; text: string; createdAt: string }) {
  return (
    <div className="flex items-center justify-center gap-2 py-0.5">
      <span className="h-px flex-1 bg-line" />
      <span className="inline-flex items-center gap-1.5 rounded-full bg-elevated px-2.5 py-1 text-[11px] font-medium text-muted">
        <span className="text-muted/80">{icon}</span>
        {text}
        <span className="tabnums opacity-70">· {formatClock(createdAt)}</span>
      </span>
      <span className="h-px flex-1 bg-line" />
    </div>
  );
}
