import { Bot, Clock, AlertCircle } from "lucide-react";
import { cn } from "@/lib/cn";
import { formatClock } from "@/lib/format";
import { messageAuthor } from "@/lib/timeline";
import type { MessageSender, MessageStatus } from "@/lib/types";

interface Props {
  sender: MessageSender;
  direction: "inbound" | "outbound";
  text: string;
  createdAt: string;
  status?: MessageStatus;
}

// Пузырь сообщения. Лид — слева (нейтральный), исходящие — справа (teal).
// Подпись исходящего: «Бот» (авто) или «Anna» (ручной ответ) — см. messageAuthor.
// status (ручная отправка): sending — часы, failed — «не отправлено».
export function MessageBubble({ sender, direction, text, createdAt, status }: Props) {
  const outbound = direction === "outbound";
  const author = messageAuthor(sender);
  const isBot = sender === "anna";
  const failed = status === "failed";
  return (
    <div className={cn("flex", outbound ? "justify-end" : "justify-start")}>
      <div
        className={cn(
          "max-w-[80%] rounded-2xl px-3 py-2 text-[15px] leading-snug shadow-soft",
          outbound
            ? failed
              ? "rounded-br-md border border-danger/40 bg-danger-bg text-ink"
              : "rounded-br-md bg-primary text-on-primary"
            : "rounded-bl-md border border-line bg-surface text-ink",
        )}
      >
        {outbound && author && (
          <span className={cn(
            "mb-0.5 flex items-center gap-1 text-[11px] font-semibold",
            failed ? "text-danger" : "opacity-75",
          )}>
            {isBot && <Bot size={11} />}
            {author}
          </span>
        )}
        <span className="whitespace-pre-wrap break-words">{text}</span>
        <span
          className={cn(
            "mt-0.5 flex items-center justify-end gap-1 text-[10px] tabnums",
            failed ? "text-danger" : outbound ? "text-on-primary/70" : "text-muted",
          )}
        >
          {status === "sending" && <Clock size={10} />}
          {failed && <><AlertCircle size={10} /> не отправлено ·</>}
          {formatClock(createdAt)}
        </span>
      </div>
    </div>
  );
}
