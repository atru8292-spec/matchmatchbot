import { useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { ImageOff, X } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { cn } from "@/lib/cn";
import { relativeTime } from "@/lib/format";
import type { LeadPhoto } from "@/lib/types";

// Вердикт Vision → короткая подпись + тон (для бейджа на превью).
const VERDICT: Record<string, { label: string; cls: string }> = {
  ok: { label: "Проверено", cls: "bg-success-bg text-success" },
  payment_ok: { label: "Оплата ок", cls: "bg-success-bg text-success" },
  reject: { label: "Отклонено", cls: "bg-danger-bg text-danger" },
  manual: { label: "На проверке", cls: "bg-accent-bg text-accent-ink" },
};

export function PhotoGallery({ photos }: { photos: LeadPhoto[] }) {
  const [open, setOpen] = useState<LeadPhoto | null>(null);

  if (photos.length === 0) {
    return (
      <Card className="flex flex-col items-center gap-2 px-4 py-8 text-center">
        <ImageOff size={24} className="text-muted" />
        <p className="text-sm text-muted">Лид пока не присылал фото.</p>
      </Card>
    );
  }

  return (
    <div>
      <h2 className="px-1 pb-2 text-xs font-semibold uppercase tracking-wide text-muted">
        Фото ({photos.length})
      </h2>
      <div className="grid grid-cols-3 gap-2">
        {photos.map((p, i) => {
          const v = p.verdict ? VERDICT[p.verdict] : undefined;
          return (
            <button key={p.url + i} onClick={() => setOpen(p)} className="text-left">
              <div className="aspect-square overflow-hidden rounded-card border border-line bg-elevated">
                <img src={p.url} alt="Фото лида" loading="lazy"
                  className="h-full w-full object-cover transition-transform duration-150 ease-standard hover:scale-105" />
              </div>
              {/* Плашка вердикта — ПОД фото, подписью (не перекрывает изображение) */}
              {v && (
                <span className={cn("mt-1 inline-block rounded-full px-1.5 py-0.5 text-[10px] font-medium", v.cls)}>
                  {v.label}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {/* Лайтбокс — крупное фото по тапу */}
      <Dialog.Root open={open !== null} onOpenChange={(o) => !o && setOpen(null)}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-40 bg-ink/80 backdrop-blur-sm" />
          <Dialog.Content className="fixed inset-0 z-50 flex flex-col items-center justify-center p-4 focus:outline-none">
            <Dialog.Title className="sr-only">Фото лида</Dialog.Title>
            <Dialog.Close
              className="absolute right-4 top-4 flex h-10 w-10 items-center justify-center rounded-full bg-surface/90 text-ink"
              aria-label="Закрыть"
            >
              <X size={20} />
            </Dialog.Close>
            {open && (
              <>
                <img src={open.url} alt="Фото лида"
                  className="max-h-[80vh] max-w-full rounded-card object-contain shadow-lift" />
                <div className="mt-3 flex items-center gap-2 text-sm text-on-primary/90">
                  {open.verdict && VERDICT[open.verdict] && (
                    <span className={cn("rounded-full px-2 py-0.5 text-xs font-medium", VERDICT[open.verdict].cls)}>
                      {VERDICT[open.verdict].label}
                    </span>
                  )}
                  {open.receivedAt && (
                    <span className="text-white/70">{relativeTime(open.receivedAt)} назад</span>
                  )}
                </div>
              </>
            )}
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
