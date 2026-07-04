import { useQuery } from "@tanstack/react-query";
import { RefreshCw, AlertTriangle, CheckCircle2, ChevronRight, CloudOff } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { Skeleton } from "@/components/ui/Skeleton";
import { fetchStats } from "@/lib/api";
import { stageMeta } from "@/lib/stages";
import { formatPhone } from "@/lib/format";
import { cn } from "@/lib/cn";
import type { EscalationItem, FunnelStat } from "@/lib/types";
import type { StageTone as _T } from "@/lib/stages";

// Тон стадии → сплошной цвет полосы воронки (base-цвета из токенов).
const BAR: Record<_T, string> = {
  neutral: "bg-neutral", info: "bg-info", accent: "bg-accent",
  primary: "bg-primary", success: "bg-success", danger: "bg-danger",
};

interface Props {
  onOpenLead: (phone: string) => void;
}

export function StatsScreen({ onOpenLead }: Props) {
  const { data, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["stats"],
    queryFn: fetchStats,
  });

  return (
    <div className="flex flex-col">
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-line bg-paper/90 px-4 py-3 backdrop-blur">
        <h1 className="text-xl text-ink">Статистика</h1>
        <Button variant="ghost" size="sm" aria-label="Обновить" className="px-2" onClick={() => refetch()}>
          <RefreshCw size={18} className={cn(isFetching && "animate-spin")} />
        </Button>
      </header>

      {isPending ? (
        <StatsSkeleton />
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
        <div className="space-y-3 p-3">
          {/* Эскалации — самое важное, наверху */}
          <EscalationsCard data={data.pendingEscalations} onOpenLead={onOpenLead} />

          {/* KPI: новые лиды */}
          <div className="grid grid-cols-3 gap-2">
            <Kpi label="Сегодня" value={data.newToday} accent />
            <Kpi label="За неделю" value={data.newWeek} />
            <Kpi label="Всего" value={data.totalLeads} />
          </div>

          {/* Воронка */}
          <Card className="p-4">
            <h2 className="mb-3 font-display text-sm font-semibold text-ink">Воронка по стадиям</h2>
            {data.funnel.length === 0 ? (
              <p className="py-4 text-center text-sm text-muted">Пока нет лидов.</p>
            ) : (
              <div className="space-y-2.5">
                {data.funnel.map((f) => <FunnelBar key={f.stage} stat={f} />)}
              </div>
            )}
          </Card>
        </div>
      )}
    </div>
  );
}

function Kpi({ label, value, accent }: { label: string; value: number; accent?: boolean }) {
  return (
    <Card className={cn("p-3", accent && "border-accent/40")}>
      <div className={cn("font-display text-2xl font-bold tabnums", accent ? "text-accent-ink" : "text-ink")}>
        {value}
      </div>
      <div className="mt-0.5 text-xs text-muted">{label}</div>
    </Card>
  );
}

function FunnelBar({ stat }: { stat: FunnelStat }) {
  const tone = stageMeta(stat.stage).tone as _T;
  return (
    <div>
      <div className="mb-1 flex items-baseline justify-between gap-2 text-sm">
        <span className="truncate text-ink">{stat.label}</span>
        <span className="shrink-0 text-muted tabnums">
          <span className="font-semibold text-ink">{stat.total}</span> · {stat.percent}%
        </span>
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-elevated">
        <div
          className={cn("h-full rounded-full", BAR[tone])}
          style={{ width: `${Math.max(stat.percent, 2)}%` }}
        />
      </div>
    </div>
  );
}

function EscalationsCard({
  data, onOpenLead,
}: { data: { count: number; items: EscalationItem[] }; onOpenLead: (phone: string) => void }) {
  if (data.count === 0) {
    return (
      <Card className="flex items-center gap-3 p-4">
        <CheckCircle2 size={22} className="shrink-0 text-success" />
        <div>
          <div className="font-display text-sm font-semibold text-ink">Нет зависших диалогов</div>
          <div className="text-xs text-muted">Все эскалации обработаны.</div>
        </div>
      </Card>
    );
  }
  return (
    <Card className="overflow-hidden border-danger/40">
      <div className="flex items-center gap-2 bg-danger-bg px-4 py-2.5">
        <AlertTriangle size={18} className="shrink-0 text-danger" />
        <span className="text-sm font-semibold text-danger">
          Ждут ответа: {data.count}
        </span>
      </div>
      <div className="divide-y divide-line">
        {data.items.map((e) => (
          <button
            key={e.phone}
            onClick={() => onOpenLead(e.phone)}
            className="flex w-full items-center gap-3 px-4 py-2.5 text-left transition-colors duration-150 ease-standard hover:bg-elevated"
          >
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium text-ink">
                {e.name || formatPhone(e.phone)}
              </div>
              {e.reason && <div className="truncate text-xs text-muted">{e.reason}</div>}
            </div>
            {typeof e.minutesLeft === "number" && (
              <span className="shrink-0 text-xs text-muted tabnums">{waitLabel(e.minutesLeft)}</span>
            )}
            <ChevronRight size={16} className="shrink-0 text-muted" />
          </button>
        ))}
      </div>
    </Card>
  );
}

// minutes_left: <=0 → просрочено, иначе «осталось Nч/Nм».
function waitLabel(min: number): string {
  if (min <= 0) return "просрочено";
  if (min < 60) return `${min} мин`;
  return `${Math.round(min / 60)} ч`;
}

function StatsSkeleton() {
  return (
    <div className="space-y-3 p-3">
      <Skeleton className="h-16 w-full rounded-card" />
      <div className="grid grid-cols-3 gap-2">
        <Skeleton className="h-16 rounded-card" />
        <Skeleton className="h-16 rounded-card" />
        <Skeleton className="h-16 rounded-card" />
      </div>
      <Skeleton className="h-48 w-full rounded-card" />
    </div>
  );
}
