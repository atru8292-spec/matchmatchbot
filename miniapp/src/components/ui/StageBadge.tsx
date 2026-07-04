import { cn } from "@/lib/cn";
import { stageMeta, TONE_CLASS } from "@/lib/stages";
import type { FunnelStage } from "@/lib/types";

interface Props {
  stage: FunnelStage;
  label?: string; // если лейбл пришёл с бэкенда — используем его
  className?: string;
}

// Бейдж стадии воронки: приглушённый тон + точка. Для быстрого сканирования списка.
export function StageBadge({ stage, label, className }: Props) {
  const meta = stageMeta(stage);
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium",
        TONE_CLASS[meta.tone],
        className,
      )}
    >
      <span className="h-1.5 w-1.5 rounded-full bg-current opacity-70" />
      {label ?? meta.label}
    </span>
  );
}
