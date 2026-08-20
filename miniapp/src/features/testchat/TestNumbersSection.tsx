import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FlaskConical, Trash2, RotateCcw, Plus } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { fetchTestNumbers, testNumberActions } from "@/lib/api";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/cn";
import type { TestNumber } from "@/lib/types";

const KEY = ["testNumbers"];

// Тестовые bypass-номера: бот отвечает им даже при глобальной паузе (bot_paused=1) —
// для ручного тестирования вживую в WhatsApp, без ожидания реальных лидов. "Сброс"
// сносит номеру ВСЮ историю (лид+сообщения+фото), чтобы тестировать с чистого листа.
export function TestNumbersSection() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [confirmAction, setConfirmAction] = useState<{ kind: "reset" | "remove"; n: TestNumber } | null>(null);

  const { data, isPending } = useQuery({ queryKey: KEY, queryFn: fetchTestNumbers });
  const numbers = data ?? [];

  const resetM = useMutation({
    mutationFn: (phone: string) => testNumberActions.reset(phone),
  });
  const removeM = useMutation({
    mutationFn: (phone: string) => testNumberActions.remove(phone),
    onSuccess: (res) => qc.setQueryData(KEY, res.numbers),
  });

  const confirm = () => {
    if (!confirmAction) return;
    if (confirmAction.kind === "reset") resetM.mutate(confirmAction.n.phone);
    else removeM.mutate(confirmAction.n.phone);
    setConfirmAction(null);
  };

  return (
    <Card className="space-y-2.5 p-3">
      <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-muted">
        <FlaskConical size={13} /> Тестовые номера
      </div>
      <p className="text-xs leading-relaxed text-muted">
        Бот отвечает им, даже когда пауза включена всем остальным. «Сброс» сносит всю историю номера —
        следующее сообщение бот встретит как совершенно нового лида.
      </p>

      {!isPending && numbers.length > 0 && (
        <div className="divide-y divide-line rounded-control border border-line">
          {numbers.map((n) => (
            <div key={n.phone} className="flex items-center gap-2 px-3 py-2">
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm font-medium text-ink">{n.label || "Без названия"}</div>
                <div className="text-xs text-muted tabnums">{formatPhone(n.phone)}</div>
              </div>
              <button
                onClick={() => setConfirmAction({ kind: "reset", n })}
                aria-label="Сбросить историю"
                title="Сбросить историю"
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-control text-muted transition-colors hover:bg-accent-bg hover:text-accent-ink"
              >
                <RotateCcw size={16} />
              </button>
              <button
                onClick={() => setConfirmAction({ kind: "remove", n })}
                aria-label="Убрать из тестовых"
                title="Убрать из тестовых"
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-control text-muted transition-colors hover:bg-danger-bg hover:text-danger"
              >
                <Trash2 size={16} />
              </button>
            </div>
          ))}
        </div>
      )}

      <AddTestNumberForm open={open} setOpen={setOpen} />

      <ConfirmDialog
        open={confirmAction !== null}
        onOpenChange={(o) => !o && setConfirmAction(null)}
        title={confirmAction?.kind === "reset" ? "Сбросить историю?" : "Убрать из тестовых?"}
        description={
          confirmAction?.kind === "reset" ? (
            <>Вся переписка, фото и стадии{" "}
              <span className="font-medium text-ink">{confirmAction?.n.label || formatPhone(confirmAction?.n.phone ?? "")}</span>
              {" "}будут удалены безвозвратно. Номер останется в тестовых.</>
          ) : (
            <>Бот перестанет отвечать{" "}
              <span className="font-medium text-ink">{confirmAction?.n.label || formatPhone(confirmAction?.n.phone ?? "")}</span>
              {" "}на паузе, как всем остальным.</>
          )
        }
        confirmLabel={confirmAction?.kind === "reset" ? "Сбросить" : "Убрать"}
        danger
        onConfirm={confirm}
      />
    </Card>
  );
}

function AddTestNumberForm({ open, setOpen }: { open: boolean; setOpen: (v: boolean) => void }) {
  const qc = useQueryClient();
  const [phone, setPhone] = useState("");
  const [label, setLabel] = useState("");

  const addM = useMutation({
    mutationFn: () => testNumberActions.add(phone.trim(), label.trim() || undefined),
    onSuccess: (res) => {
      qc.setQueryData(KEY, res.numbers);
      setPhone(""); setLabel(""); setOpen(false);
    },
  });

  const canSubmit = phone.replace(/\D/g, "").length >= 8 && !addM.isPending;

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className={cn(
          "flex w-full items-center justify-center gap-2 rounded-control border border-dashed border-line py-2.5",
          "text-sm font-medium text-muted transition-colors duration-150 ease-standard hover:border-primary/50 hover:text-primary",
        )}
      >
        <Plus size={16} /> Добавить тестовый номер
      </button>
    );
  }

  return (
    <div className="space-y-2">
      <Input value={phone} onChange={(e) => setPhone(e.target.value)}
        placeholder="Телефон, напр. +7 963 570 8880" inputMode="tel" autoFocus />
      <Input value={label} onChange={(e) => setLabel(e.target.value)}
        placeholder="Имя (напр. Мила) — необязательно" />
      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={() => { setOpen(false); setPhone(""); setLabel(""); }}>
          Отмена
        </Button>
        <Button variant="primary" size="sm" disabled={!canSubmit} onClick={() => addM.mutate()}>
          {addM.isPending ? "Добавление…" : "Добавить"}
        </Button>
      </div>
    </div>
  );
}
