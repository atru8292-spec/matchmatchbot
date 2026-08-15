import { useState } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/cn";
import { formatClock, relativeTime } from "@/lib/format";
import { VERDICT } from "@/lib/photoVerdict";
import type { LeadPhoto } from "@/lib/types";

// Фото лида прямо в ленте Истории (вместо голого текста "[photo received]" —
// это внутренний плейсхолдер для AI-контекста, не то, что должна видеть Аня).
// Тап открывает тот же лайтбокс, что и в PhotoGallery (вкладка Профиль).
export function PhotoMessageBubble({ photo, createdAt }: { photo: LeadPhoto; createdAt: string }) {
  const [open, setOpen] = useState(false);
  const v = photo.verdict ? VERDICT[photo.verdict] : undefined;

  return (
    <div className="flex justify-start">
      <div className="max-w-[65%]">
        <button
          onClick={() => setOpen(true)}
          className="block overflow-hidden rounded-2xl rounded-bl-md border border-line bg-elevated shadow-soft"
        >
          <img src={photo.url} alt="Фото лида" loading="lazy" className="max-h-64 w-full object-cover" />
        </button>
        <div className="mt-1 flex items-center gap-1.5 px-1">
          {v && (
            <span className={cn("rounded-full px-1.5 py-0.5 text-[10px] font-medium", v.cls)}>{v.label}</span>
          )}
          <span className="text-[10px] tabnums text-muted">{formatClock(createdAt)}</span>
        </div>
      </div>

      <Dialog.Root open={open} onOpenChange={setOpen}>
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
            <img src={photo.url} alt="Фото лида"
              className="max-h-[80vh] max-w-full rounded-card object-contain shadow-lift" />
            <div className="mt-3 flex items-center gap-2 text-sm text-on-primary/90">
              {v && <span className={cn("rounded-full px-2 py-0.5 text-xs font-medium", v.cls)}>{v.label}</span>}
              {photo.receivedAt && <span className="text-white/70">{relativeTime(photo.receivedAt)} назад</span>}
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  );
}
