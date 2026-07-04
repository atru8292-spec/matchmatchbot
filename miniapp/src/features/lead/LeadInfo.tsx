import { Star } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { StageBadge } from "@/components/ui/StageBadge";
import { relativeTime } from "@/lib/format";
import type { LeadDetail } from "@/lib/types";

const INTEREST_LABEL: Record<string, string> = {
  event: "Ивент",
  agency: "Агентство",
  both: "Ивент + агентство",
};

function Fact({ label, value }: { label: string; value: string | null }) {
  if (!value) return null;
  return (
    <div className="flex items-baseline justify-between gap-3 py-1.5">
      <span className="shrink-0 text-sm text-muted">{label}</span>
      <span className="min-w-0 truncate text-right text-sm font-medium text-ink">{value}</span>
    </div>
  );
}

// Панель ключевых фактов о лиде. Пустые поля не показываем (не плодим «—»).
export function LeadInfo({ lead }: { lead: LeadDetail }) {
  return (
    <Card className="p-4">
      <div className="mb-2 flex items-center justify-between">
        <StageBadge stage={lead.funnelStage} label={lead.funnelStageLabel} />
        {lead.isClient && (
          <span className="inline-flex items-center gap-1 rounded-full bg-accent-bg px-2 py-0.5 text-xs font-medium text-accent-ink">
            <Star size={12} /> Клиент из списка
          </span>
        )}
      </div>

      {/* Анкетные поля лида */}
      <div className="divide-y divide-line">
        <Fact label="Интерес" value={lead.interest ? INTEREST_LABEL[lead.interest] ?? lead.interest : null} />
        <Fact label="Возраст" value={lead.age ? `${lead.age}` : null} />
        <Fact label="Профессия" value={lead.profession} />
        <Fact label="Город" value={lead.city} />
        {lead.isClient && lead.clientReason && (
          <Fact label="Причина (клиент)" value={lead.clientReason} />
        )}
      </div>

      {/* Активность — отдельно от анкеты */}
      <div className="mt-3 border-t border-line pt-2">
        <div className="pb-1 text-[11px] font-semibold uppercase tracking-wide text-muted">
          Активность
        </div>
        <div className="divide-y divide-line">
          <Fact label="Первое сообщение" value={lead.firstMessageAt ? relativeTime(lead.firstMessageAt) + " назад" : null} />
          <Fact label="Последнее сообщение" value={lead.lastMessageAt ? relativeTime(lead.lastMessageAt) + " назад" : null} />
        </div>
      </div>
    </Card>
  );
}
