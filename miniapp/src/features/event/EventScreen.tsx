import { useRef, useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CalendarHeart, MapPin, CreditCard, GraduationCap, Image as ImageIcon, Check, CloudOff, UploadCloud, Loader2, X, Tag, Table2 } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Switch } from "@/components/ui/Switch";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { cn } from "@/lib/cn";
import { fetchEvent, saveEvent, uploadInvitation } from "@/lib/api";
import type { EventSettings } from "@/lib/types";
import { DayOfReminder } from "./DayOfReminder";
import { EventMediaSection } from "./EventMediaSection";

const KEY = ["event"];
const todayStr = () => new Date().toISOString().slice(0, 10);
const isUrl = (v: string) => /^https?:\/\//i.test(v.trim());

export function EventScreen() {
  const { data, isPending, isError, refetch } = useQuery({ queryKey: KEY, queryFn: fetchEvent });

  return (
    <div className="flex h-full flex-col">
      <header className="sticky top-0 z-10 border-b border-line bg-paper/90 px-4 py-3 backdrop-blur">
        <h1 className="text-xl text-ink">Ивент</h1>
      </header>
      {isPending ? (
        <FormSkeleton />
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
        <EventForm initial={data} />
      )}
    </div>
  );
}

function EventForm({ initial }: { initial: EventSettings }) {
  const qc = useQueryClient();
  const [active, setActive] = useState(initial.eventActive);
  const [date, setDate] = useState(initial.eventDate);
  const [start, setStart] = useState(initial.eventStart);
  const [end, setEnd] = useState(initial.eventEnd);
  const [address, setAddress] = useState(initial.eventAddress);
  const [priceNonmember, setPriceNonmember] = useState(initial.eventPriceNonmember);
  const [eventLink, setEventLink] = useState(initial.eventLink);
  const [courseLink, setCourseLink] = useState(initial.courseLink);
  const [invUrl, setInvUrl] = useState(initial.invitationUrl);
  const [invReady, setInvReady] = useState(initial.invitationReady);
  const [guestTab, setGuestTab] = useState(initial.eventGuestTab);
  const [saved, setSaved] = useState(false);

  const saveM = useMutation({
    mutationFn: (s: EventSettings) => saveEvent(s),
    onSuccess: (res) => {
      qc.setQueryData(KEY, res);
      qc.invalidateQueries({ queryKey: KEY });
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2500);
    },
  });

  // Смена URL картинки → сбрасываем «отправлять» (как /set_invitation в боте:
  // после нового URL нужно заново подтвердить, чтобы не отправить не ту картинку).
  const onInvUrl = (v: string) => {
    setInvUrl(v);
    if (v.trim() !== initial.invitationUrl.trim() && invReady) setInvReady(false);
  };

  // Загрузка файла картинки → Storage → URL в поле (как хранятся фото лидов).
  const fileRef = useRef<HTMLInputElement>(null);
  const uploadM = useMutation({
    mutationFn: (file: File) => uploadInvitation(file),
    onSuccess: (url) => onInvUrl(url),
  });
  const pickFile = (file: File | undefined | null) => {
    if (file && file.type.startsWith("image/")) uploadM.mutate(file);
  };

  // Валидация (фронт): дата не в прошлом, ссылки — URL, обязательные при активном ивенте.
  const err: Record<string, string> = {};
  if (active) {
    if (!date) err.date = "Укажите дату";
    else if (date < todayStr()) err.date = "Дата не может быть в прошлом";
    if (!start.trim()) err.start = "Укажите время начала";
    if (!address.trim()) err.address = "Укажите адрес";
  } else if (date && date < todayStr()) {
    err.date = "Дата не может быть в прошлом";
  }
  const priceOk = (v: string) => /^[\d.,\s]+$/.test(v.trim());
  if (priceNonmember.trim() && !priceOk(priceNonmember)) err.priceNonmember = "Только цифры";
  if (eventLink.trim() && !isUrl(eventLink)) err.eventLink = "Нужен URL (http…)";
  if (courseLink.trim() && !isUrl(courseLink)) err.courseLink = "Нужен URL (http…)";
  if (invUrl.trim() && !isUrl(invUrl)) err.invUrl = "Нужен URL (http…)";
  if (invReady && !invUrl.trim()) err.invReady = "Задайте URL картинки";
  const hasErrors = Object.keys(err).length > 0;

  const onSave = () => {
    if (hasErrors || saveM.isPending) return;
    saveM.mutate({
      eventActive: active, eventDate: date,
      eventStart: start.trim(), eventEnd: end.trim(),
      eventTime: start.trim(),  // зеркало для #15/#47/#54 (бэк тоже дублирует)
      eventAddress: address.trim(),
      eventPriceMember: "", eventPriceNonmember: priceNonmember.trim(),
      eventPriceOld: "",
      eventLink: eventLink.trim(), courseLink: courseLink.trim(),
      invitationUrl: invUrl.trim(), invitationReady: invReady,
      eventGuestTab: guestTab.trim(),
    });
  };

  return (
    <>
      <div className="flex-1 space-y-3 overflow-y-auto p-3">
        {/* Детали ивента */}
        <Card className="space-y-3 p-4">
          <SwitchRow
            icon={<CalendarHeart size={18} />}
            label="Ивент активен"
            hint="Выключен — напоминания не рассылаются"
            checked={active}
            onChange={setActive}
          />
          <Field label="Дата" required={active} error={err.date}>
            <Input type="date" value={date} min={todayStr()} onChange={(e) => setDate(e.target.value)} />
          </Field>
          <div className="grid grid-cols-2 gap-2">
            <Field label="Время начала" required={active} error={err.start}>
              <Input value={start} onChange={(e) => setStart(e.target.value)} placeholder="8:30 PM" />
            </Field>
            <Field label="Время окончания">
              <Input value={end} onChange={(e) => setEnd(e.target.value)} placeholder="12:00 AM" />
            </Field>
          </div>
          <Field label="Адрес" required={active} error={err.address}>
            <Input
              value={address}
              onChange={(e) => setAddress(e.target.value)}
              placeholder="Roma Norte, Ciudad de México"
              leading={<MapPin size={18} />}
            />
          </Field>
          <Field label="Цена билета (MXN)" error={err.priceNonmember} hint="Единая цена для всех">
            <Input value={priceNonmember} onChange={(e) => setPriceNonmember(e.target.value)}
              placeholder="6,000" inputMode="numeric" leading={<Tag size={18} />} />
          </Field>
          <Field label="Вкладка гостевого списка"
            hint="Точное имя вкладки в книге Ани, куда бот впишет оплативших (напр. «22 de Julio»)">
            <Input value={guestTab} onChange={(e) => setGuestTab(e.target.value)}
              placeholder="22 de Julio" leading={<Table2 size={18} />} />
          </Field>
        </Card>

        {/* Ссылки */}
        <Card className="space-y-3 p-4">
          <Field label="Ссылка на оплату / бронь" error={err.eventLink}
            hint="Попадёт в напоминания об ивенте">
            <Input value={eventLink} onChange={(e) => setEventLink(e.target.value)}
              placeholder="https://…" inputMode="url" leading={<CreditCard size={18} />} />
          </Field>
          <Field label="Ссылка на курсы" error={err.courseLink}
            hint="Предлагается в отказных сценариях">
            <Input value={courseLink} onChange={(e) => setCourseLink(e.target.value)}
              placeholder="https://…" inputMode="url" leading={<GraduationCap size={18} />} />
          </Field>
        </Card>

        {/* Приглашение */}
        <Card className="space-y-3 p-4">
          <div className="text-sm font-medium text-ink">Картинка-приглашение</div>

          {/* Зона загрузки: клик или drag-and-drop */}
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => pickFile(e.target.files?.[0])}
          />
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            onDragOver={(e) => e.preventDefault()}
            onDrop={(e) => { e.preventDefault(); pickFile(e.dataTransfer.files?.[0]); }}
            disabled={uploadM.isPending}
            className={cn(
              "flex w-full flex-col items-center justify-center gap-1.5 rounded-control border border-dashed border-line py-6",
              "text-sm text-muted transition-colors duration-150 ease-standard hover:border-primary/50 hover:text-primary",
              "disabled:opacity-60",
            )}
          >
            {uploadM.isPending ? (
              <><Loader2 size={20} className="animate-spin" /> Загрузка…</>
            ) : (
              <><UploadCloud size={20} /> Перетащите картинку или нажмите, чтобы выбрать</>
            )}
          </button>

          {uploadM.isError && (
            <p className="text-xs text-danger">Не удалось загрузить файл. Попробуйте другой.</p>
          )}
          {err.invUrl && <p className="text-xs text-danger">{err.invUrl}</p>}

          {/* Превью загруженной картинки */}
          {isUrl(invUrl) && (
            <div className="relative">
              <img src={invUrl} alt="Превью приглашения"
                className="max-h-48 w-full rounded-control border border-line object-cover" />
              <button
                type="button"
                onClick={() => onInvUrl("")}
                aria-label="Убрать картинку"
                className="absolute right-2 top-2 flex h-8 w-8 items-center justify-center rounded-full bg-surface/90 text-ink shadow-soft"
              >
                <X size={16} />
              </button>
            </div>
          )}

          <SwitchRow
            icon={<ImageIcon size={18} />}
            label="Отправлять приглашение"
            hint={
              err.invReady
                ? undefined
                : invUrl.trim() !== initial.invitationUrl.trim()
                  ? "Проверьте картинку и подтвердите отправку"
                  : "Бот приложит картинку к приглашению"
            }
            error={err.invReady}
            checked={invReady}
            onChange={setInvReady}
            disabled={!invUrl.trim()}
          />
        </Card>

        {/* Медиа с ивентов: загрузка фото/видео (бот шлёт лидам) */}
        <EventMediaSection />

        {/* Напоминание дня ивента: предпросмотр шаблонов + ручная отправка */}
        <DayOfReminder />
      </div>

      {/* Липкий футер сохранения */}
      <div className="sticky bottom-0 flex items-center gap-3 border-t border-line bg-surface/95 px-4 py-3 backdrop-blur"
        style={{ paddingBottom: "max(0.75rem, env(safe-area-inset-bottom))" }}>
        {saved && (
          <span className="inline-flex items-center gap-1 text-sm font-medium text-success">
            <Check size={16} /> Сохранено
          </span>
        )}
        <Button className="ml-auto" variant="primary" size="md"
          onClick={onSave} disabled={hasErrors || saveM.isPending}>
          {saveM.isPending ? "Сохранение…" : "Сохранить"}
        </Button>
      </div>
    </>
  );
}

function Field({
  label, required, error, hint, children,
}: { label: string; required?: boolean; error?: string; hint?: string; children: ReactNode }) {
  return (
    <div>
      <label className="mb-1 flex items-center gap-1 text-sm font-medium text-ink">
        {label}
        {required && <span className="text-danger">*</span>}
      </label>
      {children}
      {error ? (
        <p className="mt-1 text-xs text-danger">{error}</p>
      ) : hint ? (
        <p className="mt-1 text-xs text-muted">{hint}</p>
      ) : null}
    </div>
  );
}

function SwitchRow({
  icon, label, hint, error, checked, onChange, disabled,
}: {
  icon: ReactNode; label: string; hint?: string; error?: string;
  checked: boolean; onChange: (v: boolean) => void; disabled?: boolean;
}) {
  return (
    <div className="flex items-center gap-3">
      <span className="text-muted">{icon}</span>
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium text-ink">{label}</div>
        {error ? (
          <div className="text-xs text-danger">{error}</div>
        ) : hint ? (
          <div className="text-xs text-muted">{hint}</div>
        ) : null}
      </div>
      <Switch checked={checked} onChange={onChange} disabled={disabled} aria-label={label} />
    </div>
  );
}

function FormSkeleton() {
  return (
    <div className="space-y-3 p-3">
      <Skeleton className="h-44 w-full rounded-card" />
      <Skeleton className="h-32 w-full rounded-card" />
      <Skeleton className="h-40 w-full rounded-card" />
    </div>
  );
}
