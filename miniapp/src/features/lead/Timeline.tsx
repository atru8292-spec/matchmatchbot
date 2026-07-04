import { Milestone, UserCog, Bot, Ban, RotateCcw, Star, StarOff } from "lucide-react";
import { formatDay } from "@/lib/format";
import { ACTION_TEXT, stageChangeText } from "@/lib/timeline";
import type { TimelineAction, TimelineItem } from "@/lib/types";
import { MessageBubble } from "./MessageBubble";
import { SystemRow } from "./SystemRow";
import { NoteCard } from "./NoteCard";

const ACTION_ICON: Record<TimelineAction, typeof Bot> = {
  takeover: UserCog,
  release: Bot,
  stop: Ban,
  resume: RotateCcw,
  client_add: Star,
  client_remove: StarOff,
};

// Единый таймлайн: сообщения, смены стадий, действия и заметки в одном потоке
// по времени (старое сверху, новое снизу — как в мессенджере), с разбивкой по дням.
export function Timeline({ items }: { items: TimelineItem[] }) {
  let lastDay = "";
  return (
    <div className="space-y-2">
      {items.map((item) => {
        const day = formatDay(item.createdAt);
        const showDay = day !== lastDay;
        lastDay = day;
        return (
          <div key={`${item.kind}-${item.id}`} className="space-y-2">
            {showDay && (
              <div className="flex justify-center py-1">
                <span className="rounded-full bg-elevated px-2.5 py-0.5 text-[11px] font-medium text-muted">
                  {day}
                </span>
              </div>
            )}
            {renderItem(item)}
          </div>
        );
      })}
    </div>
  );
}

function renderItem(item: TimelineItem) {
  switch (item.kind) {
    case "message":
      return <MessageBubble sender={item.sender} direction={item.direction} text={item.text} createdAt={item.createdAt} status={item.status} />;
    case "stage":
      return <SystemRow icon={<Milestone size={12} />} text={stageChangeText(item.fromStage, item.toStage)} createdAt={item.createdAt} />;
    case "action": {
      const Icon = ACTION_ICON[item.action];
      return <SystemRow icon={<Icon size={12} />} text={ACTION_TEXT[item.action]} createdAt={item.createdAt} />;
    }
    case "note":
      return <NoteCard text={item.text} createdAt={item.createdAt} />;
  }
}
