import { Bot, UserCog, MessageCircle, Star, StarOff, Ban, RotateCcw } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import type { LeadMode } from "@/lib/types";

interface Props {
  phone: string;
  mode: LeadMode;
  isClient: boolean;
  stopped: boolean;
  busy?: boolean; // мутация в полёте — блокируем действия, чтобы не дёргать дважды
  onMode: (m: LeadMode) => void;
  onToggleClient: () => void;
  onToggleStop: () => void;
}

// Панель действий с карточки — всё одним тапом (принцип «меньше кликов»).
// Терминология из блока 11: «Отвечает бот» / «Общаюсь лично», «Клиент из списка»,
// «Бот больше не пишет». Действия — реальные мутации (см. LeadCard), busy их блокирует.
export function LeadActions({ phone, mode, isClient, stopped, busy, onMode, onToggleClient, onToggleStop }: Props) {
  const waLink = `https://wa.me/${phone.replace(/\D/g, "")}`;

  return (
    <div className="space-y-2.5">
      {/* Сегмент: кто ведёт диалог — бот или менеджер */}
      <div className="grid grid-cols-2 gap-1 rounded-control bg-elevated p-1">
        {(
          [
            { m: "auto" as const, label: "Отвечает бот", icon: Bot },
            { m: "manual" as const, label: "Общаюсь лично", icon: UserCog },
          ]
        ).map(({ m, label, icon: Icon }) => {
          const active = mode === m && !stopped;
          return (
            <button
              key={m}
              onClick={() => onMode(m)}
              disabled={stopped || busy}
              className={cn(
                "flex h-10 items-center justify-center gap-1.5 rounded-[0.5rem] text-sm font-medium",
                "transition-colors duration-150 ease-standard disabled:opacity-50",
                active ? "bg-surface text-primary shadow-soft" : "text-muted hover:text-ink",
              )}
            >
              <Icon size={16} /> {label}
            </button>
          );
        })}
      </div>

      {/* Вторичные действия */}
      <div className="grid grid-cols-2 gap-2">
        <a
          href={waLink}
          target="_blank"
          rel="noreferrer"
          className="inline-flex h-11 items-center justify-center gap-2 rounded-control border border-line bg-surface px-4 text-[15px] font-medium text-ink transition-colors duration-150 ease-standard hover:bg-elevated focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40"
        >
          <MessageCircle size={16} /> WhatsApp
        </a>
        <Button
          variant="secondary"
          size="md"
          disabled={busy}
          icon={isClient ? <StarOff size={16} /> : <Star size={16} />}
          onClick={onToggleClient}
        >
          {isClient ? "Убрать из клиентов" : "В клиенты"}
        </Button>
      </div>

      {/* Стоп-бот / вернуть */}
      {stopped ? (
        <div className="flex items-center justify-between gap-3 rounded-control bg-danger-bg px-3 py-2.5">
          <span className="text-sm font-medium text-danger">Бот больше не пишет</span>
          <Button variant="secondary" size="sm" disabled={busy} icon={<RotateCcw size={15} />} onClick={onToggleStop}>
            Вернуть боту
          </Button>
        </div>
      ) : (
        <Button variant="danger" size="md" disabled={busy} icon={<Ban size={16} />} onClick={onToggleStop} className="w-full">
          Остановить бота для этого лида
        </Button>
      )}
    </div>
  );
}
