import { Star, UserCog } from "lucide-react";
import { Avatar } from "@/components/ui/Avatar";
import { StageBadge } from "@/components/ui/StageBadge";
import { relativeTime, formatPhone, initials, senderPrefix } from "@/lib/format";
import type { Lead } from "@/lib/types";

interface Props {
  lead: Lead;
  onOpen: (phone: string) => void;
}

// Строка лида (WhatsApp-подобная плотность): имя/превью/время + мета-линия
// со стадией, телефоном и пометками (вручную / клиент из списка).
export function LeadRow({ lead, onOpen }: Props) {
  const name = lead.name || lead.whatsappName || "Без имени";
  return (
    <button
      onClick={() => onOpen(lead.phone)}
      className="flex w-full items-start gap-3 px-4 py-3 text-left transition-colors duration-150 ease-standard hover:bg-elevated focus-visible:bg-elevated focus-visible:outline-none"
    >
      <Avatar initials={initials(lead.name || lead.whatsappName, lead.phone)} stage={lead.funnelStage} />

      <div className="min-w-0 flex-1">
        {/* Строка 1: имя + время последнего сообщения */}
        <div className="flex items-baseline gap-2">
          <span className="min-w-0 flex-1 truncate font-display text-[15px] font-semibold text-ink">
            {name}
          </span>
          <span className="shrink-0 text-xs text-muted tabnums">
            {relativeTime(lead.lastMessageAt)}
          </span>
        </div>

        {/* Строка 2: превью последнего сообщения */}
        <p className="mt-0.5 truncate text-sm text-muted">
          {lead.lastMessagePreview ? (
            <>
              <span className="text-ink/60">{senderPrefix(lead.lastMessageSender)}</span>
              {lead.lastMessagePreview}
            </>
          ) : (
            <span className="italic opacity-70">нет сообщений</span>
          )}
        </p>

        {/* Строка 3: стадия + пометки + телефон */}
        <div className="mt-1.5 flex items-center gap-1.5">
          <StageBadge stage={lead.funnelStage} label={lead.funnelStageLabel} />
          {lead.mode === "manual" && (
            <span className="inline-flex items-center gap-1 rounded-full bg-elevated px-1.5 py-0.5 text-[11px] font-medium text-muted">
              <UserCog size={11} /> Вручную
            </span>
          )}
          {lead.isClient && (
            <span className="inline-flex items-center gap-1 rounded-full bg-accent-bg px-1.5 py-0.5 text-[11px] font-medium text-accent-ink">
              <Star size={11} /> Клиент
            </span>
          )}
          <span className="ml-auto shrink-0 text-[11px] text-muted tabnums">
            {formatPhone(lead.phone)}
          </span>
        </div>
      </div>
    </button>
  );
}
