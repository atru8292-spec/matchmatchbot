import { StickyNote } from "lucide-react";
import { formatClock } from "@/lib/format";

// Заметка в таймлайне — внутренняя пометка (лиду не уходит). Отличается от чат-пузырей
// тёплой золотой рамкой и подписью «Заметка». Без автора (только текст + время).
export function NoteCard({ text, createdAt }: { text: string; createdAt: string }) {
  return (
    <div className="rounded-card border border-accent/40 bg-accent-bg/60 px-3 py-2">
      <div className="mb-0.5 flex items-center justify-between">
        <span className="inline-flex items-center gap-1 text-[11px] font-semibold text-accent-ink">
          <StickyNote size={12} /> Заметка
        </span>
        <span className="text-[10px] text-muted tabnums">{formatClock(createdAt)}</span>
      </div>
      <p className="whitespace-pre-wrap break-words text-sm text-ink">{text}</p>
    </div>
  );
}
