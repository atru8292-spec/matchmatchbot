import { cn } from "@/lib/cn";
import { stageMeta, TONE_CLASS } from "@/lib/stages";
import type { FunnelStage } from "@/lib/types";

interface Props {
  initials: string;
  stage: FunnelStage; // тонируем аватар в тон стадии — доп. визуальный якорь
  className?: string;
}

// Круг с инициалами, тонированный по стадии. Без фото — экономно и узнаваемо.
export function Avatar({ initials, stage, className }: Props) {
  const meta = stageMeta(stage);
  return (
    <div
      className={cn(
        "flex h-11 w-11 shrink-0 items-center justify-center rounded-xl",
        "font-display text-sm font-semibold",
        TONE_CLASS[meta.tone],
        className,
      )}
    >
      {initials}
    </div>
  );
}
