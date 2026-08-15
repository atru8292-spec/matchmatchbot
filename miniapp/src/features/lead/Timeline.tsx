import { Milestone, UserCog, Bot, Ban, RotateCcw, Star, StarOff } from "lucide-react";
import { formatDay } from "@/lib/format";
import { ACTION_TEXT, stageChangeText } from "@/lib/timeline";
import type { LeadPhoto, TimelineAction, TimelineItem } from "@/lib/types";
import { MessageBubble } from "./MessageBubble";
import { PhotoMessageBubble } from "./PhotoMessageBubble";
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

// Плейсхолдер, который normalize.py кладёт в messages.text за входящее фото (нужен
// AI как лёгкий текстовый маркер истории) — лиду/Ане это не должно быть видно как есть.
const PHOTO_PLACEHOLDER = "[photo received]";

function isPhotoPlaceholder(item: TimelineItem): boolean {
  return item.kind === "message" && item.direction === "inbound" && item.text === PHOTO_PLACEHOLDER;
}

// Единый таймлайн: сообщения, смены стадий, действия и заметки в одном потоке
// по времени (старое сверху, новое снизу — как в мессенджере), с разбивкой по дням.
// photos сопоставляются с "[photo received]"-заглушками ПОЗИЦИОННО, по порядку —
// у messages/lead_photos нет общего id, но обе стороны пишутся в том же порядке,
// в котором лид прислал фото (см. main._process_photos).
export function Timeline({ items, photos }: { items: TimelineItem[]; photos: LeadPhoto[] }) {
  const photoQueue = [...photos].sort((a, b) => a.receivedAt.localeCompare(b.receivedAt));
  let photoIdx = 0;
  let lastDay = "";
  return (
    <div className="space-y-2">
      {items.map((item) => {
        const day = formatDay(item.createdAt);
        const showDay = day !== lastDay;
        lastDay = day;
        const photo = isPhotoPlaceholder(item) ? photoQueue[photoIdx++] : undefined;
        return (
          <div key={`${item.kind}-${item.id}`} className="space-y-2">
            {showDay && (
              <div className="flex justify-center py-1">
                <span className="rounded-full bg-elevated px-2.5 py-0.5 text-[11px] font-medium text-muted">
                  {day}
                </span>
              </div>
            )}
            {renderItem(item, photo)}
          </div>
        );
      })}
    </div>
  );
}

function renderItem(item: TimelineItem, photo?: LeadPhoto) {
  switch (item.kind) {
    case "message":
      if (photo) return <PhotoMessageBubble photo={photo} createdAt={item.createdAt} />;
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
