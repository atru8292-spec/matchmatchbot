import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, CloudOff, Send, Loader2, User, MessagesSquare } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { cn } from "@/lib/cn";
import { fetchLeadDetail, leadActions, sendMessage } from "@/lib/api";
import { formatPhone, initials } from "@/lib/format";
import type { LeadDetail, LeadMode, LeadPhoto, TimelineItem } from "@/lib/types";
import { LeadInfo } from "./LeadInfo";
import { LeadActions } from "./LeadActions";
import { NoteComposer } from "./NoteComposer";
import { NoteCard } from "./NoteCard";
import { Timeline } from "./Timeline";
import { PhotoGallery } from "./PhotoGallery";

type CardTab = "profile" | "history";

// Аватар лида = последнее по дате фото с вердиктом «Проверено» (ok). Иначе null → инициалы.
function verifiedAvatar(photos: LeadPhoto[]): string | null {
  const ok = photos
    .filter((p) => p.verdict === "ok" && p.url)
    .sort((a, b) => (b.receivedAt || "").localeCompare(a.receivedAt || ""));
  return ok[0]?.url ?? null;
}

interface Props {
  phone: string;
  onBack: () => void;
}

// Загрузчик карточки: скелетон / ошибка с retry / контент.
export function LeadCard({ phone, onBack }: Props) {
  const { data, isPending, isError, refetch } = useQuery({
    queryKey: ["lead", phone],
    queryFn: () => fetchLeadDetail(phone),
  });

  const name = data ? data.name || data.whatsappName || "Без имени" : "Лид";
  const avatarUrl = data ? verifiedAvatar(data.photos) : null;

  return (
    <div className="flex h-full flex-col bg-paper">
      <Header title={name} phone={phone} avatarUrl={avatarUrl} onBack={onBack} />
      {isPending ? (
        <CardSkeleton />
      ) : isError || !data ? (
        <div className="flex flex-1 items-center justify-center">
          <EmptyState
            icon={<CloudOff size={26} />}
            title="Не удалось загрузить"
            description="Проверьте соединение и попробуйте снова."
            action={<Button variant="secondary" size="sm" onClick={() => refetch()}>Повторить</Button>}
          />
        </div>
      ) : (
        <LeadCardInner detail={data} />
      )}
    </div>
  );
}

function Header({
  title, phone, avatarUrl, onBack,
}: { title: string; phone: string; avatarUrl?: string | null; onBack: () => void }) {
  return (
    <header className="sticky top-0 z-10 flex items-center gap-2 border-b border-line bg-paper/90 px-2 py-2 backdrop-blur">
      <button
        onClick={onBack}
        aria-label="Назад"
        className="flex h-10 w-10 shrink-0 items-center justify-center rounded-control text-ink hover:bg-elevated"
      >
        <ArrowLeft size={20} />
      </button>
      {/* Аватар из проверенного фото (или инициалы) */}
      {avatarUrl ? (
        <img src={avatarUrl} alt="" className="h-10 w-10 shrink-0 rounded-xl border border-line object-cover" />
      ) : (
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-elevated font-display text-sm font-semibold text-muted">
          {initials(title === "Лид" ? null : title, phone)}
        </div>
      )}
      <div className="min-w-0">
        <h1 className="truncate text-lg text-ink">{title}</h1>
        <p className="text-xs text-muted tabnums">{formatPhone(phone)}</p>
      </div>
    </header>
  );
}

// Контент с реальными мутациями. Оптимистичный патч факта (мгновенный отклик) →
// инвалидация ["lead", phone] (сверка с БД + таймлайн подтянет системную строку
// действия, залогированную в manager_actions). Ошибка → откат к предыдущему детейлу.
function LeadCardInner({ detail }: { detail: LeadDetail }) {
  const qc = useQueryClient();
  const phone = detail.phone;
  const key = ["lead", phone];
  const [tab, setTab] = useState<CardTab>("history");

  // Фабрика оптимистичной мутации: patch применяется к детейлу сразу, при ошибке —
  // откат, в конце — инвалидация (перечитать факты + таймлайн из БД).
  const optimistic = (mutationFn: () => Promise<unknown>, patch: Partial<LeadDetail>) => ({
    mutationFn,
    onMutate: async () => {
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<LeadDetail>(key);
      if (prev) qc.setQueryData<LeadDetail>(key, { ...prev, ...patch });
      return { prev };
    },
    onError: (_e: unknown, _v: void, ctx: { prev?: LeadDetail } | undefined) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: key }),
  });

  const takeoverM = useMutation(optimistic(() => leadActions.takeover(phone), { mode: "manual" }));
  const releaseM = useMutation(optimistic(() => leadActions.release(phone), { mode: "auto" }));
  const stopM = useMutation(optimistic(() => leadActions.stop(phone), { doNotContact: true }));
  const resumeM = useMutation(optimistic(() => leadActions.resume(phone), { doNotContact: false, mode: "auto" }));
  const wlAddM = useMutation(optimistic(() => leadActions.addWhitelist(phone), { isClient: true }));
  const wlRemoveM = useMutation(optimistic(() => leadActions.removeWhitelist(phone), { isClient: false }));
  const noteM = useMutation({
    mutationFn: (text: string) => leadActions.addNote(phone, text),
    onSettled: () => qc.invalidateQueries({ queryKey: key }),
  });

  // Ручная отправка: оптимистично добавляем сообщение (Anna, «отправляется») +
  // переводим на manual (авто-takeover на бэке); на settle — инвалидация (статус
  // sent|failed из БД). Ошибка — откат.
  const sendM = useMutation({
    mutationFn: (text: string) => sendMessage(phone, text),
    onMutate: async (text: string) => {
      await qc.cancelQueries({ queryKey: key });
      const prev = qc.getQueryData<LeadDetail>(key);
      if (prev) {
        const optimistic: TimelineItem = {
          kind: "message", id: `tmp-${prev.timeline.length}`, sender: "manager",
          direction: "outbound", text, createdAt: new Date().toISOString(), status: "sending",
        };
        qc.setQueryData<LeadDetail>(key, {
          ...prev, mode: "manual", timeline: [...prev.timeline, optimistic],
        });
      }
      return { prev };
    },
    onError: (_e: unknown, _t: string, ctx: { prev?: LeadDetail } | undefined) => {
      if (ctx?.prev) qc.setQueryData(key, ctx.prev);
    },
    onSettled: () => qc.invalidateQueries({ queryKey: key }),
  });

  const busy = [takeoverM, releaseM, stopM, resumeM, wlAddM, wlRemoveM].some((m) => m.isPending);

  const onMode = (m: LeadMode) => {
    if (m === detail.mode || busy) return;
    (m === "manual" ? takeoverM : releaseM).mutate();
  };
  const onToggleClient = () => (detail.isClient ? wlRemoveM : wlAddM).mutate();
  const onToggleStop = () => (detail.doNotContact ? resumeM : stopM).mutate();

  // Кнопки действий — общие для обеих вкладок (не дублируем разметку).
  const actions = (
    <LeadActions
      phone={phone}
      mode={detail.mode}
      isClient={detail.isClient}
      stopped={detail.doNotContact}
      busy={busy}
      onMode={onMode}
      onToggleClient={onToggleClient}
      onToggleStop={onToggleStop}
    />
  );

  // Последняя заметка лида (для блока внизу «Профиля»).
  const notes = detail.timeline.filter(
    (i): i is Extract<TimelineItem, { kind: "note" }> => i.kind === "note",
  );
  const lastNote = notes[notes.length - 1];

  return (
    <>
      <TabSwitch tab={tab} onChange={setTab} />

      {tab === "profile" ? (
        // Профиль — данные лида + действия + галерея фото + последняя заметка. Без переписки.
        <div className="flex-1 space-y-3 overflow-y-auto p-3">
          <LeadInfo lead={detail} />
          {actions}
          <PhotoGallery photos={detail.photos} />
          {lastNote && (
            <div>
              <h2 className="px-1 pb-1 text-xs font-semibold uppercase tracking-wide text-muted">
                Последняя заметка
              </h2>
              <NoteCard text={lastNote.text} createdAt={lastNote.createdAt} />
            </div>
          )}
        </div>
      ) : (
        // История — управление + единый таймлайн + композер сообщения.
        <>
          <div className="flex-1 space-y-3 overflow-y-auto p-3">
            {actions}

            <NoteComposer onAdd={(t) => noteM.mutate(t)} saving={noteM.isPending} />

            <Timeline items={detail.timeline} />
          </div>

          <ChatComposer
            onSend={(t) => sendM.mutate(t)}
            sending={sendM.isPending}
            autoMode={detail.mode === "auto" && !detail.doNotContact}
          />
        </>
      )}
    </>
  );
}

// Переключатель вкладок карточки (тот же сегмент-паттерн, что в кнопках действий).
function TabSwitch({ tab, onChange }: { tab: CardTab; onChange: (t: CardTab) => void }) {
  const tabs = [
    { id: "profile" as const, label: "Профиль", icon: User },
    { id: "history" as const, label: "История", icon: MessagesSquare },
  ];
  return (
    <div className="border-b border-line bg-paper px-3 pb-2 pt-1">
      <div className="grid grid-cols-2 gap-1 rounded-control bg-elevated p-1">
        {tabs.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => onChange(id)}
            aria-current={tab === id ? "page" : undefined}
            className={cn(
              "flex h-9 items-center justify-center gap-1.5 rounded-[0.5rem] text-sm font-medium",
              "transition-colors duration-150 ease-standard",
              tab === id ? "bg-surface text-primary shadow-soft" : "text-muted hover:text-ink",
            )}
          >
            <Icon size={16} /> {label}
          </button>
        ))}
      </div>
    </div>
  );
}

// Композер ручного сообщения (sticky футер, как чат-инпут). Enter — отправить,
// Shift+Enter — перенос строки. Подсказка про авто-режим (отправка → takeover).
function ChatComposer({
  onSend, sending, autoMode,
}: { onSend: (text: string) => void; sending: boolean; autoMode: boolean }) {
  const [text, setText] = useState("");
  const submit = () => {
    const t = text.trim();
    if (!t || sending) return;
    onSend(t);
    setText("");
  };
  return (
    <div
      className="sticky bottom-0 border-t border-line bg-surface/95 backdrop-blur"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      {autoMode && (
        <p className="px-3 pt-2 text-[11px] leading-snug text-muted">
          Бот сейчас отвечает сам — отправка переведёт диалог на ручной режим.
        </p>
      )}
      <div className="flex items-end gap-2 p-2">
        <textarea
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); submit(); }
          }}
          rows={1}
          placeholder="Написать сообщение…"
          className="max-h-28 min-h-[2.75rem] flex-1 resize-none rounded-control border border-line bg-surface px-3 py-2.5 text-[15px] text-ink outline-none placeholder:text-muted focus:border-primary/60 focus:ring-2 focus:ring-primary/20"
        />
        <Button
          size="md"
          variant="primary"
          onClick={submit}
          disabled={!text.trim() || sending}
          aria-label="Отправить"
          className="h-11 px-3"
        >
          {sending ? <Loader2 size={18} className="animate-spin" /> : <Send size={18} />}
        </Button>
      </div>
    </div>
  );
}

function CardSkeleton() {
  return (
    <div className="space-y-3 p-3">
      <Skeleton className="h-40 w-full rounded-card" />
      <Skeleton className="h-11 w-full rounded-control" />
      <div className="grid grid-cols-2 gap-2">
        <Skeleton className="h-11 rounded-control" />
        <Skeleton className="h-11 rounded-control" />
      </div>
      <Skeleton className="h-16 w-2/3 rounded-2xl" />
      <Skeleton className="ml-auto h-16 w-2/3 rounded-2xl" />
    </div>
  );
}
