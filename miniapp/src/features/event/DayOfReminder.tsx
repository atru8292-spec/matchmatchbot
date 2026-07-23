import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Send, Search, AlertTriangle, Check, CloudOff, Loader2, BellRing } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Skeleton } from "@/components/ui/Skeleton";
import { cn } from "@/lib/cn";
import { fetchDayOfPreview, fetchDayOfRecipients, fetchLeads, sendDayOf } from "@/lib/api";
import type { DayOfRecipient, DayOfSendResult } from "@/lib/types";

// Совпадает с scheduler.PAID_STAGES на бэкенде — для расчёта шаблона у найденных лидов.
const PAID = new Set(["event_attended", "client_agency"]);
const defTmpl = (stage: string): "A" | "B" => (PAID.has(stage) ? "A" : "B");

type Mode = "auto" | "A" | "B";
type Row = {
  phone: string; name: string; stageLabel: string;
  template: "A" | "B"; alreadySent: boolean; sentAt: string | null;
};

const fmtDate = (iso: string | null) =>
  iso ? new Date(iso).toLocaleDateString("ru-RU", { day: "numeric", month: "long" }) : "";

export function DayOfReminder() {
  const qc = useQueryClient();
  const preview = useQuery({ queryKey: ["dayof-preview"], queryFn: fetchDayOfPreview });
  const recips = useQuery({ queryKey: ["dayof-recipients"], queryFn: fetchDayOfRecipients });

  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<Record<string, Row>>({});
  const [mode, setMode] = useState<Mode>("auto");
  const [result, setResult] = useState<DayOfSendResult | null>(null);

  // Поиск по всем лидам (не только event) — если Аня знает кого-то вне списка.
  const searchQ = useQuery({
    queryKey: ["dayof-search", search.trim()],
    queryFn: () => fetchLeads({ search: search.trim(), limit: 20 }),
    enabled: search.trim().length >= 2,
  });

  const rows: Row[] = useMemo(() => {
    if (search.trim().length >= 2) {
      return (searchQ.data?.leads ?? []).map((l) => ({
        phone: l.phone,
        name: l.name || l.whatsappName || l.phone.replace("wa_", ""),
        stageLabel: l.funnelStageLabel,
        template: defTmpl(l.funnelStage),
        alreadySent: false,
        sentAt: null,
      }));
    }
    return (recips.data?.recipients ?? []).map((r: DayOfRecipient) => ({
      phone: r.phone, name: r.name, stageLabel: r.funnelStageLabel,
      template: r.template, alreadySent: r.alreadySent, sentAt: r.sentAt,
    }));
  }, [search, searchQ.data, recips.data]);

  const sendM = useMutation({
    mutationFn: (v: { phones: string[]; force: boolean }) =>
      sendDayOf(v.phones.map((phone) => ({ phone, template: mode })), v.force),
    onSuccess: (res) => {
      setResult(res);
      recips.refetch();
      qc.invalidateQueries({ queryKey: ["dayof-recipients"] });
    },
  });

  const toggle = (row: Row) =>
    setSelected((prev) => {
      const next = { ...prev };
      if (next[row.phone]) delete next[row.phone];
      else next[row.phone] = row;
      return next;
    });

  const selectedPhones = Object.keys(selected);
  // Что реально уйдёт получателю: override (A/B) или дефолт строки (auto).
  const shownTemplate = (row: Row) => (mode === "auto" ? row.template : mode);

  const onSend = () => {
    if (!selectedPhones.length || sendM.isPending) return;
    setResult(null);
    sendM.mutate({ phones: selectedPhones, force: false });
  };
  const onResendDuplicates = () => {
    if (!result?.duplicates.length) return;
    sendM.mutate({ phones: result.duplicates.map((d) => d.phone), force: true });
  };

  return (
    <>
      {/* ── Предпросмотр шаблонов ── */}
      <Card className="space-y-3 p-4">
        <div className="flex items-center gap-2">
          <BellRing size={18} className="text-primary" />
          <div className="text-sm font-medium text-ink">Напоминание в день ивента — предпросмотр</div>
        </div>
        <p className="text-xs text-muted">Что получат люди в день ивента, с текущими значениями.</p>
        {preview.isPending ? (
          <Skeleton className="h-40 w-full rounded-card" />
        ) : preview.isError ? (
          <div className="flex items-center gap-2 text-sm text-danger"><CloudOff size={16} /> Не удалось загрузить</div>
        ) : (
          <div className="space-y-3">
            <TemplateBlock title="Шаблон A — уже оплатившим (без ссылки)" bubbles={preview.data.templateA} />
            <TemplateBlock title="Шаблон B — ещё не оплатившим (со ссылкой)" bubbles={preview.data.templateB} />
          </div>
        )}
      </Card>

      {/* ── Ручная отправка ── */}
      <Card className="space-y-3 p-4">
        <div className="flex items-center gap-2">
          <Send size={18} className="text-primary" />
          <div className="text-sm font-medium text-ink">Отправить напоминание вручную</div>
        </div>

        <Input value={search} onChange={(e) => setSearch(e.target.value)}
          placeholder="Поиск по имени или номеру…" leading={<Search size={18} />} />

        {/* Список получателей */}
        <div className="max-h-64 overflow-y-auto rounded-control border border-line">
          {recips.isPending || (search.trim().length >= 2 && searchQ.isPending) ? (
            <div className="p-3"><Skeleton className="h-20 w-full rounded-control" /></div>
          ) : rows.length === 0 ? (
            <p className="p-4 text-center text-xs text-muted">
              {search.trim().length >= 2 ? "Никого не нашли" : "Пока нет лидов, записанных на ивент. Найдите человека через поиск."}
            </p>
          ) : (
            rows.map((row) => {
              const checked = !!selected[row.phone];
              return (
                <button key={row.phone} type="button" onClick={() => toggle(row)}
                  className={cn("flex w-full items-center gap-3 border-b border-line px-3 py-2 text-left last:border-0",
                    "transition-colors hover:bg-primary/5", checked && "bg-primary/10")}>
                  <span className={cn("flex h-5 w-5 flex-shrink-0 items-center justify-center rounded border",
                    checked ? "border-primary bg-primary text-white" : "border-line")}>
                    {checked && <Check size={14} />}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-sm text-ink">{row.name}</div>
                    <div className="text-xs text-muted">{row.stageLabel}</div>
                  </div>
                  <span className={cn("rounded px-1.5 py-0.5 text-xs font-medium",
                    shownTemplate(row) === "A" ? "bg-emerald-100 text-emerald-700" : "bg-sky-100 text-sky-700")}>
                    Шаблон {shownTemplate(row)}
                  </span>
                  {row.alreadySent && (
                    <span className="rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-700">отправлено</span>
                  )}
                </button>
              );
            })
          )}
        </div>

        {/* Переопределение шаблона */}
        <div>
          <div className="mb-1 text-xs font-medium text-muted">Шаблон</div>
          <div className="flex gap-1">
            {(["auto", "A", "B"] as Mode[]).map((m) => (
              <button key={m} type="button" onClick={() => setMode(m)}
                className={cn("flex-1 rounded-control border py-1.5 text-sm transition-colors",
                  mode === m ? "border-primary bg-primary/10 text-primary" : "border-line text-muted hover:border-primary/40")}>
                {m === "auto" ? "Авто по статусу" : m}
              </button>
            ))}
          </div>
        </div>

        {/* Предупреждение о дублях */}
        {result && result.duplicates.length > 0 && (
          <div className="space-y-2 rounded-control border border-amber-300 bg-amber-50 p-3">
            <div className="flex items-center gap-2 text-sm font-medium text-amber-800">
              <AlertTriangle size={16} /> Уже отправлено ранее
            </div>
            {result.duplicates.map((d) => (
              <div key={d.phone} className="text-xs text-amber-800">
                {d.name} — уже получил напоминание {fmtDate(d.sentAt)}.
              </div>
            ))}
            <Button variant="secondary" size="sm" onClick={onResendDuplicates} disabled={sendM.isPending}>
              Отправить повторно ({result.duplicates.length})
            </Button>
          </div>
        )}

        {/* Итог отправки */}
        {result && (result.sent.length > 0 || result.failed.length > 0) && (
          <div className="space-y-1 text-xs">
            {result.sent.length > 0 && (
              <div className="flex items-center gap-1 text-success"><Check size={14} /> Отправлено: {result.sent.length}</div>
            )}
            {result.failed.map((f) => (
              <div key={f.phone} className="text-danger">✕ {f.name}: {f.reason}</div>
            ))}
          </div>
        )}

        <Button className="w-full" variant="primary" size="md"
          onClick={onSend} disabled={!selectedPhones.length || sendM.isPending}>
          {sendM.isPending ? <><Loader2 size={16} className="animate-spin" /> Отправка…</>
            : `Отправить (${selectedPhones.length})`}
        </Button>
      </Card>
    </>
  );
}

function TemplateBlock({ title, bubbles }: { title: string; bubbles: string[] }) {
  return (
    <div>
      <div className="mb-1.5 text-xs font-medium text-muted">{title}</div>
      <div className="space-y-1.5">
        {bubbles.length === 0 ? (
          <div className="text-xs text-danger">Шаблон не задан</div>
        ) : (
          bubbles.map((b, i) => (
            <div key={i} className="rounded-2xl rounded-tl-sm bg-primary/5 px-3 py-2 text-sm text-ink">{b}</div>
          ))
        )}
      </div>
    </div>
  );
}
