import type { ReactNode } from "react";
import * as Dialog from "@radix-ui/react-dialog";
import { Button } from "./Button";

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: ReactNode;
  confirmLabel: string;
  onConfirm: () => void;
  danger?: boolean;
  pending?: boolean;
}

// Модалка подтверждения (Radix Dialog). Для необратимых действий — удаление и т.п.
// Анимации — только opacity (ок для prefers-reduced-motion).
export function ConfirmDialog({
  open, onOpenChange, title, description, confirmLabel, onConfirm, danger, pending,
}: Props) {
  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-ink/40 backdrop-blur-[2px]" />
        <Dialog.Content className="fixed left-1/2 top-1/2 z-50 w-[min(92vw,22rem)] -translate-x-1/2 -translate-y-1/2 rounded-card border border-line bg-surface p-5 shadow-lift focus:outline-none">
          <Dialog.Title className="font-display text-lg font-semibold text-ink">
            {title}
          </Dialog.Title>
          {description && (
            <Dialog.Description className="mt-1.5 text-sm leading-relaxed text-muted">
              {description}
            </Dialog.Description>
          )}
          <div className="mt-5 flex justify-end gap-2">
            <Dialog.Close asChild>
              <Button variant="secondary" size="sm">Отмена</Button>
            </Dialog.Close>
            <Button
              variant={danger ? "danger" : "primary"}
              size="sm"
              onClick={onConfirm}
              disabled={pending}
            >
              {pending ? "…" : confirmLabel}
            </Button>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
