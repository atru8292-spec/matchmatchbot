import { useMemo, useState } from "react";
import { useQuery, keepPreviousData } from "@tanstack/react-query";
import { Search, Download, X, Inbox, RefreshCw, CloudOff } from "lucide-react";
import { Input } from "@/components/ui/Input";
import { Card } from "@/components/ui/Card";
import { Button } from "@/components/ui/Button";
import { EmptyState } from "@/components/ui/EmptyState";
import { LeadRowSkeleton } from "@/components/ui/Skeleton";
import { cn } from "@/lib/cn";
import { fetchLeads, downloadLeadsExport } from "@/lib/api";
import { ACTIVE_STAGES, stageMeta } from "@/lib/stages";
import { useDebounced } from "@/lib/useDebounced";
import type { FunnelStage } from "@/lib/types";
import { LeadRow } from "./LeadRow";

interface Props {
  onOpenLead: (phone: string) => void;
}

export function LeadsScreen({ onOpenLead }: Props) {
  const [search, setSearch] = useState("");
  const [stages, setStages] = useState<FunnelStage[]>([]);
  const debouncedSearch = useDebounced(search, 250);

  const query = useMemo(
    () => ({ search: debouncedSearch, stage: stages, sort: "recent" as const }),
    [debouncedSearch, stages],
  );

  const { data, isPending, isError, isFetching, refetch } = useQuery({
    queryKey: ["leads", query],
    queryFn: () => fetchLeads(query),
    placeholderData: keepPreviousData,
  });

  const toggleStage = (s: FunnelStage) =>
    setStages((prev) => (prev.includes(s) ? prev.filter((x) => x !== s) : [...prev, s]));

  const [exporting, setExporting] = useState(false);
  const onExport = async () => {
    if (exporting) return;
    setExporting(true);
    try {
      await downloadLeadsExport(query);
    } catch {
      // сеть/сервер — тихо снимаем индикатор; кнопку можно нажать снова
    } finally {
      setExporting(false);
    }
  };

  const leads = data?.leads ?? [];
  const total = data?.total ?? 0;

  return (
    <div className="flex flex-col">
      {/* ---- Шапка (липкая) ---- */}
      <header className="sticky top-0 z-10 border-b border-line bg-paper/90 backdrop-blur">
        <div className="flex items-center justify-between px-4 pt-4 pb-2">
          <div className="flex items-baseline gap-2">
            <h1 className="text-xl text-ink">Лиды</h1>
            <span className="text-sm text-muted tabnums">{total}</span>
          </div>
          <div className="flex items-center gap-1">
            <Button
              variant="ghost"
              size="sm"
              aria-label="Обновить"
              onClick={() => refetch()}
              className="px-2"
            >
              <RefreshCw size={18} className={cn(isFetching && "animate-spin")} />
            </Button>
            <Button
              variant="secondary"
              size="sm"
              icon={<Download size={16} className={cn(exporting && "animate-pulse")} />}
              onClick={onExport}
              disabled={exporting || total === 0}
            >
              {exporting ? "Экспорт…" : "Экспорт"}
            </Button>
          </div>
        </div>

        <div className="px-4 pb-2">
          <Input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Имя или телефон"
            inputMode="search"
            leading={<Search size={18} />}
            trailing={
              search ? (
                <button onClick={() => setSearch("")} aria-label="Очистить" className="text-muted">
                  <X size={16} />
                </button>
              ) : null
            }
          />
        </div>

        {/* Фильтр-чипы по стадии (горизонтальный скролл) */}
        <div className="flex gap-2 overflow-x-auto px-4 pb-3 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
          {ACTIVE_STAGES.map((s) => {
            const on = stages.includes(s);
            return (
              <button
                key={s}
                onClick={() => toggleStage(s)}
                className={cn(
                  "shrink-0 rounded-full border px-3 py-1 text-xs font-medium transition-colors duration-150 ease-standard",
                  on
                    ? "border-primary bg-primary text-on-primary"
                    : "border-line bg-surface text-muted hover:text-ink",
                )}
              >
                {stageMeta(s).label}
              </button>
            );
          })}
        </div>
      </header>

      {/* ---- Список ---- */}
      <div className="p-3">
        <Card className="divide-y divide-line overflow-hidden">
          {isPending ? (
            Array.from({ length: 6 }).map((_, i) => <LeadRowSkeleton key={i} />)
          ) : isError ? (
            // Ошибка загрузки — НЕ пустое состояние: список есть, просто не догрузился.
            <EmptyState
              icon={<CloudOff size={26} />}
              title="Не удалось загрузить"
              description="Проверьте соединение и попробуйте снова."
              action={
                <Button variant="secondary" size="sm" icon={<RefreshCw size={16} />} onClick={() => refetch()}>
                  Повторить
                </Button>
              }
            />
          ) : leads.length === 0 ? (
            <EmptyState
              icon={<Inbox size={26} />}
              title="Лидов не найдено"
              description={
                search || stages.length
                  ? "Попробуйте изменить поиск или сбросить фильтры."
                  : "Здесь появятся новые лиды из WhatsApp."
              }
              action={
                search.length > 0 || stages.length > 0 ? (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => {
                      setSearch("");
                      setStages([]);
                    }}
                  >
                    Сбросить фильтры
                  </Button>
                ) : undefined
              }
            />
          ) : (
            leads.map((lead) => <LeadRow key={lead.phone} lead={lead} onOpen={onOpenLead} />)
          )}
        </Card>
      </div>
    </div>
  );
}
