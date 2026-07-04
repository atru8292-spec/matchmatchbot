import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { UserCheck, Trash2, Plus, Search, X, ShieldCheck } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { EmptyState } from "@/components/ui/EmptyState";
import { LeadRowSkeleton } from "@/components/ui/Skeleton";
import { ConfirmDialog } from "@/components/ui/ConfirmDialog";
import { fetchClients, clientActions } from "@/lib/api";
import { formatPhone, relativeTime, initials } from "@/lib/format";
import { useDebounced } from "@/lib/useDebounced";
import { cn } from "@/lib/cn";
import type { WhitelistClient } from "@/lib/types";

const KEY = ["clients"];

export function ClientsScreen() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const debounced = useDebounced(search, 200);
  const [toRemove, setToRemove] = useState<WhitelistClient | null>(null);

  const { data, isPending, isError, refetch } = useQuery({
    queryKey: KEY,
    queryFn: fetchClients,
  });

  const removeM = useMutation({
    mutationFn: (phone: string) => clientActions.remove(phone),
    onMutate: async (phone) => {
      await qc.cancelQueries({ queryKey: KEY });
      const prev = qc.getQueryData<WhitelistClient[]>(KEY);
      qc.setQueryData<WhitelistClient[]>(KEY, (old) => (old ?? []).filter((c) => c.phone !== phone));
      return { prev };
    },
    onError: (_e, _p, ctx) => { if (ctx?.prev) qc.setQueryData(KEY, ctx.prev); },
    onSettled: () => qc.invalidateQueries({ queryKey: KEY }),
  });

  const clients = data ?? [];
  const filtered = useMemo(() => {
    const s = debounced.trim().toLowerCase();
    if (!s) return clients;
    return clients.filter(
      (c) => (c.name || "").toLowerCase().includes(s) || c.phone.includes(s.replace(/\D/g, "")),
    );
  }, [clients, debounced]);

  const confirmRemove = () => {
    if (toRemove) removeM.mutate(toRemove.phone);
    setToRemove(null);
  };

  return (
    <div className="flex flex-col">
      <header className="sticky top-0 z-10 border-b border-line bg-paper/90 backdrop-blur">
        <div className="flex items-baseline gap-2 px-4 pt-4 pb-2">
          <h1 className="text-xl text-ink">Клиенты</h1>
          <span className="text-sm text-muted tabnums">{clients.length}</span>
        </div>
        <p className="px-4 pb-2 text-xs leading-relaxed text-muted">
          Бот им не пишет — общение ведёт Аня лично.
        </p>
        <div className="px-4 pb-3">
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Имя или телефон"
            inputMode="search"
            leading={<Search size={18} />}
            trailing={search ? (
              <button onClick={() => setSearch("")} aria-label="Очистить" className="text-muted"><X size={16} /></button>
            ) : null}
          />
        </div>
      </header>

      <div className="space-y-3 p-3">
        <AddClientForm />

        <Card className="divide-y divide-line overflow-hidden">
          {isPending ? (
            Array.from({ length: 4 }).map((_, i) => <LeadRowSkeleton key={i} />)
          ) : isError ? (
            <EmptyState
              icon={<UserCheck size={26} />}
              title="Не удалось загрузить"
              description="Проверьте соединение и попробуйте снова."
              action={<Button variant="secondary" size="sm" onClick={() => refetch()}>Повторить</Button>}
            />
          ) : filtered.length === 0 ? (
            <EmptyState
              icon={<ShieldCheck size={26} />}
              title={search ? "Ничего не найдено" : "Список клиентов пуст"}
              description={search ? "Измените запрос." : "Добавьте клиента по номеру телефона выше."}
            />
          ) : (
            filtered.map((c) => (
              <ClientRow key={c.phone} client={c} onRemove={() => setToRemove(c)} />
            ))
          )}
        </Card>
      </div>

      <ConfirmDialog
        open={toRemove !== null}
        onOpenChange={(o) => !o && setToRemove(null)}
        title="Убрать из клиентов?"
        description={
          <>Бот снова начнёт отвечать{" "}
            <span className="font-medium text-ink">{toRemove?.name || formatPhone(toRemove?.phone ?? "")}</span>.
          </>
        }
        confirmLabel="Убрать"
        danger
        onConfirm={confirmRemove}
      />
    </div>
  );
}

function ClientRow({ client, onRemove }: { client: WhitelistClient; onRemove: () => void }) {
  const name = client.name || "Без имени";
  return (
    <div className="flex items-center gap-3 px-4 py-3">
      <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-xl bg-accent-bg font-display text-sm font-semibold text-accent-ink">
        {initials(client.name, client.phone)}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate font-display text-[15px] font-semibold text-ink">{name}</div>
        <div className="text-sm text-muted tabnums">{formatPhone(client.phone)}</div>
        <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-xs text-muted">
          {client.reason && <span className="text-accent-ink">{client.reason}</span>}
          <span>{client.addedBy}</span>
          {client.addedAt && <span>· {relativeTime(client.addedAt)} назад</span>}
        </div>
      </div>
      <button
        onClick={onRemove}
        aria-label="Убрать из клиентов"
        className="flex h-9 w-9 shrink-0 items-center justify-center rounded-control text-muted transition-colors duration-150 ease-standard hover:bg-danger-bg hover:text-danger"
      >
        <Trash2 size={18} />
      </button>
    </div>
  );
}

function AddClientForm() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [phone, setPhone] = useState("");
  const [reason, setReason] = useState("");

  const addM = useMutation({
    mutationFn: () => clientActions.add(phone.trim(), reason.trim() || undefined),
    onMutate: async () => {
      await qc.cancelQueries({ queryKey: KEY });
      const prev = qc.getQueryData<WhitelistClient[]>(KEY);
      const optimistic: WhitelistClient = {
        phone: "wa_" + phone.replace(/\D/g, ""), name: null,
        reason: reason.trim() || null, addedBy: "…", addedAt: new Date().toISOString(),
      };
      qc.setQueryData<WhitelistClient[]>(KEY, (old) => [optimistic, ...(old ?? [])]);
      return { prev };
    },
    onError: (_e, _v, ctx) => { if (ctx?.prev) qc.setQueryData(KEY, ctx.prev); },
    onSuccess: () => { setPhone(""); setReason(""); setOpen(false); },
    onSettled: () => qc.invalidateQueries({ queryKey: KEY }),
  });

  const canSubmit = phone.replace(/\D/g, "").length >= 8 && !addM.isPending;

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className={cn(
          "flex w-full items-center justify-center gap-2 rounded-control border border-dashed border-line py-3",
          "text-sm font-medium text-muted transition-colors duration-150 ease-standard hover:border-primary/50 hover:text-primary",
        )}
      >
        <Plus size={16} /> Добавить клиента
      </button>
    );
  }

  return (
    <Card className="space-y-2.5 p-3">
      <div className="flex items-center gap-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
        <UserCheck size={13} /> Новый клиент
      </div>
      <Input
        value={phone}
        onChange={(e) => setPhone(e.target.value)}
        placeholder="Телефон, напр. +52 155 1234 5678"
        inputMode="tel"
        autoFocus
      />
      <Input
        value={reason}
        onChange={(e) => setReason(e.target.value)}
        placeholder="Причина (необязательно)"
      />
      <div className="flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={() => { setOpen(false); setPhone(""); setReason(""); }}>
          Отмена
        </Button>
        <Button variant="primary" size="sm" disabled={!canSubmit} onClick={() => addM.mutate()}>
          {addM.isPending ? "Добавление…" : "Добавить"}
        </Button>
      </div>
    </Card>
  );
}
