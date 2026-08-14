import { useState, type ReactNode } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Check, Clock, CloudOff } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { Select } from "@/components/ui/Select";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { fetchAssignees, saveAssignees } from "@/lib/api";
import { listTimezones } from "@/lib/timezones";
import type { AssigneeSchedule, AssigneesResponse } from "@/lib/types";

const KEY = ["assignees"];
const TIMEZONES = listTimezones();

export function ScheduleScreen() {
  const { data, isPending, isError, refetch } = useQuery({ queryKey: KEY, queryFn: fetchAssignees });

  return (
    <div className="flex h-full flex-col">
      <header className="sticky top-0 z-10 border-b border-line bg-paper/90 px-4 py-3 backdrop-blur">
        <h1 className="text-xl text-ink">Расписание звонков</h1>
        <p className="mt-0.5 text-xs text-muted">
          Кто из троих и когда доступна — бот ставит звонки по приоритету: Аня → Мила → Рита.
        </p>
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
        <ScheduleForm initial={data} />
      )}
    </div>
  );
}

function ScheduleForm({ initial }: { initial: AssigneesResponse }) {
  const qc = useQueryClient();
  const [rows, setRows] = useState<AssigneeSchedule[]>(initial.assignees);
  const [saved, setSaved] = useState(false);

  const saveM = useMutation({
    mutationFn: (r: AssigneesResponse) => saveAssignees(r),
    onSuccess: (res) => {
      qc.setQueryData(KEY, res);
      qc.invalidateQueries({ queryKey: KEY });
      setSaved(true);
      window.setTimeout(() => setSaved(false), 2500);
    },
  });

  const setRow = (slug: string, patch: Partial<AssigneeSchedule>) =>
    setRows((prev) => prev.map((r) => (r.slug === slug ? { ...r, ...patch } : r)));

  // Валидация: конец должен быть позже начала (совпадает с проверкой на бэкенде).
  const errors: Record<string, string> = {};
  for (const r of rows) {
    if (r.end <= r.start) errors[r.slug] = "Время окончания должно быть позже начала";
  }
  const hasErrors = Object.keys(errors).length > 0;

  const onSave = () => {
    if (hasErrors || saveM.isPending) return;
    saveM.mutate({ assignees: rows });
  };

  return (
    <>
      <div className="flex-1 space-y-3 overflow-y-auto p-3">
        {rows.map((row) => (
          <Card key={row.slug} className="space-y-3 p-4">
            <div className="flex items-center gap-2">
              <span
                className="h-2.5 w-2.5 shrink-0 rounded-full"
                style={{ backgroundColor: row.color }}
                aria-hidden
              />
              <span className="text-sm font-medium text-ink">{row.name}</span>
            </div>
            <Field label="Часовой пояс">
              <Select
                leading={<Clock size={18} />}
                value={row.tz}
                onChange={(e) => setRow(row.slug, { tz: e.target.value })}
              >
                {TIMEZONES.map((tz) => (
                  <option key={tz} value={tz}>{tz}</option>
                ))}
              </Select>
            </Field>
            <div className="grid grid-cols-2 gap-2">
              <Field label="Доступна с">
                <input
                  type="time"
                  value={row.start}
                  onChange={(e) => setRow(row.slug, { start: e.target.value })}
                  className="h-11 w-full rounded-control border border-line bg-surface px-3 text-[15px] text-ink outline-none focus:border-primary/60 focus:ring-2 focus:ring-primary/20"
                />
              </Field>
              <Field label="До" error={errors[row.slug]}>
                <input
                  type="time"
                  value={row.end}
                  onChange={(e) => setRow(row.slug, { end: e.target.value })}
                  className="h-11 w-full rounded-control border border-line bg-surface px-3 text-[15px] text-ink outline-none focus:border-primary/60 focus:ring-2 focus:ring-primary/20"
                />
              </Field>
            </div>
            <p className="text-xs text-muted">Время указано по её местному часовому поясу.</p>
          </Card>
        ))}
      </div>

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
  label, error, children,
}: { label: string; error?: string; children: ReactNode }) {
  return (
    <div>
      <label className="mb-1 block text-sm font-medium text-ink">{label}</label>
      {children}
      {error && <p className="mt-1 text-xs text-danger">{error}</p>}
    </div>
  );
}

function FormSkeleton() {
  return (
    <div className="space-y-3 p-3">
      <Skeleton className="h-40 w-full rounded-card" />
      <Skeleton className="h-40 w-full rounded-card" />
      <Skeleton className="h-40 w-full rounded-card" />
    </div>
  );
}
