import { useEffect, useRef, useState, type ReactNode } from "react";
import { Send, RotateCcw, Image as ImageIcon, Bot, AlertCircle, ChevronRight } from "lucide-react";
import { cn } from "@/lib/cn";
import { sendTestChat } from "@/lib/api";
import type { TestChatProfile, TestChatResponse } from "@/lib/types";

// Отдельный экран-песочница: гоняем сообщения через реальный пайплайн бота
// (POST /api/mini/test-chat), НИЧЕГО не пишется в БД. Память диалога (history +
// накопленный профиль) живёт здесь, в стейте, и уходит в каждый запрос.

const EMPTY_PROFILE: TestChatProfile = {
  isSingle: null, age: null, profession: null, city: null, interest: null,
  photoReceived: false, funnelStage: null, whatsappName: "Test",
};

type Debug = Pick<TestChatResponse,
  "usedScenarioId" | "usedScenarioTitle" | "action" | "needsEscalation" | "ragCandidates" | "extracted">;

type Turn =
  | { kind: "lead"; text: string }
  | { kind: "anna"; bubbles: string[]; debug: Debug };

type HistMsg = { sender: "lead" | "anna"; text: string };

// Плоская история для бэка: каждый баббл Anna — отдельная реплика (как в проде).
function toHistory(turns: Turn[]): HistMsg[] {
  const out: HistMsg[] = [];
  for (const t of turns) {
    if (t.kind === "lead") out.push({ sender: "lead", text: t.text });
    else for (const b of t.bubbles) out.push({ sender: "anna", text: b });
  }
  return out;
}

export function TestChatScreen() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const [profile, setProfile] = useState<TestChatProfile>(EMPTY_PROFILE);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, pending]);

  const reset = () => {
    if (turns.length && !window.confirm("Начать новый тест? Текущий диалог очистится.")) return;
    setTurns([]); setProfile(EMPTY_PROFILE); setInput(""); setError(null);
  };

  // Отправка: text — реальное сообщение боту; display — что показать в ленте;
  // photo=true имитирует «фото одобрено» (как прод после Vision-ok).
  const submit = async (text: string, display: string, photo = false) => {
    if (pending) return;
    const history = toHistory(turns);
    const sendProfile: TestChatProfile = photo
      ? { ...profile, photoReceived: true, funnelStage: "qualified" }
      : profile;

    setTurns((prev) => [...prev, { kind: "lead", text: display }]);
    setInput(""); setError(null); setPending(true);
    try {
      const r = await sendTestChat({ leadProfile: sendProfile, history, message: text });
      setTurns((prev) => [...prev, {
        kind: "anna", bubbles: r.messages,
        debug: {
          usedScenarioId: r.usedScenarioId, usedScenarioTitle: r.usedScenarioTitle,
          action: r.action, needsEscalation: r.needsEscalation,
          ragCandidates: r.ragCandidates, extracted: r.extracted,
        },
      }]);
      const ex = (r.extracted ?? {}) as Record<string, unknown>;
      setProfile({
        ...sendProfile,
        isSingle: (ex.is_single as boolean) ?? sendProfile.isSingle,
        age: (ex.age as number) ?? sendProfile.age,
        profession: (ex.profession as string) ?? sendProfile.profession,
        city: (ex.city as string) ?? sendProfile.city,
        interest: (ex.interest as string) ?? sendProfile.interest,
        funnelStage: r.funnelStage ?? sendProfile.funnelStage,
      });
    } catch (e) {
      // откат оптимистичной реплики лида + вернуть текст в поле (для повтора)
      setTurns((prev) => prev.slice(0, -1));
      if (!photo) setInput(text);
      setError(e instanceof Error ? e.message : "Ошибка запроса");
    } finally {
      setPending(false);
    }
  };

  const sendText = () => {
    const t = input.trim();
    if (t) submit(t, t);
  };
  const sendPhoto = () => submit("[фото одобрено]", "📸 Фото отправлено (Vision OK — симуляция)", true);

  return (
    <div className="flex h-full flex-col">
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-line bg-paper/90 px-4 py-3 backdrop-blur">
        <div>
          <h1 className="text-xl text-ink">Тест переписки</h1>
          <p className="text-[11px] text-muted">Пишешь от лица лида · в БД ничего не сохраняется</p>
        </div>
        <button
          onClick={reset}
          className="inline-flex items-center gap-1 rounded-control border border-line px-2.5 py-1.5 text-sm text-ink transition-colors hover:bg-surface"
        >
          <RotateCcw size={15} /> Новый тест
        </button>
      </header>

      {/* Лента */}
      <div className="flex-1 space-y-2 overflow-y-auto p-3">
        {turns.length === 0 && (
          <div className="mt-10 text-center text-sm text-muted">
            Напиши первое сообщение от лица лида —<br />бот ответит по реальному сценарию.
          </div>
        )}
        {turns.map((t, i) =>
          t.kind === "lead" ? (
            <div key={i} className="flex justify-start">
              <div className="max-w-[82%] rounded-2xl rounded-bl-md border border-line bg-surface px-3 py-2 text-[15px] leading-snug text-ink shadow-soft">
                <span className="whitespace-pre-wrap break-words">{t.text}</span>
              </div>
            </div>
          ) : (
            <div key={i} className="space-y-1">
              {t.bubbles.map((b, j) => (
                <div key={j} className="flex justify-end">
                  <div className="max-w-[82%] rounded-2xl rounded-br-md bg-primary px-3 py-2 text-[15px] leading-snug text-on-primary shadow-soft">
                    {j === 0 && (
                      <span className="mb-0.5 flex items-center gap-1 text-[11px] font-semibold opacity-75">
                        <Bot size={11} /> Anna
                      </span>
                    )}
                    <span className="whitespace-pre-wrap break-words">{b}</span>
                  </div>
                </div>
              ))}
              <DebugRow debug={t.debug} />
            </div>
          ),
        )}
        {pending && (
          <div className="flex justify-end">
            <div className="rounded-2xl rounded-br-md bg-primary/70 px-3 py-2 text-on-primary shadow-soft">
              <span className="inline-flex gap-1">
                <Dot /> <Dot d={0.15} /> <Dot d={0.3} />
              </span>
            </div>
          </div>
        )}
        <div ref={endRef} />
      </div>

      {/* Ввод */}
      <div
        className="sticky bottom-0 border-t border-line bg-surface/95 px-3 py-2 backdrop-blur"
        style={{ paddingBottom: "max(0.5rem, env(safe-area-inset-bottom))" }}
      >
        {error && (
          <p className="mb-1.5 flex items-center gap-1 text-xs text-danger">
            <AlertCircle size={12} /> {error}
          </p>
        )}
        <div className="flex items-end gap-2">
          <button
            onClick={sendPhoto}
            disabled={pending}
            title="Симулировать отправку фото (Vision OK)"
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-control border border-line text-muted transition-colors hover:text-primary disabled:opacity-50"
          >
            <ImageIcon size={18} />
          </button>
          <textarea
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendText(); }
            }}
            rows={1}
            placeholder="Сообщение от лица лида…"
            className="max-h-28 min-h-[2.5rem] flex-1 resize-none rounded-control border border-line bg-paper px-3 py-2 text-[15px] text-ink outline-none placeholder:text-muted focus:border-primary/50"
          />
          <button
            onClick={sendText}
            disabled={pending || !input.trim()}
            className="flex h-10 w-10 shrink-0 items-center justify-center rounded-control bg-primary text-on-primary transition-colors disabled:opacity-40"
          >
            <Send size={18} />
          </button>
        </div>
      </div>
    </div>
  );
}

// Дебаг под ответом бота: итоговый сценарий, action/эскалация, RAG top-3, extracted.
function DebugRow({ debug }: { debug: Debug }) {
  const [open, setOpen] = useState(true);
  const esc = debug.needsEscalation || debug.action === "escalate";
  const exEntries = Object.entries(debug.extracted ?? {});
  return (
    <div className="flex justify-end">
      <div className="max-w-[82%] rounded-lg border border-line bg-paper px-2.5 py-1.5 text-[11px] text-muted">
        <button onClick={() => setOpen((o) => !o)} className="flex items-center gap-1 font-medium text-ink/80">
          <ChevronRight size={12} className={cn("transition-transform", open && "rotate-90")} />
          Сценарий {debug.usedScenarioId != null ? `#${debug.usedScenarioId}` : "—"}
          {debug.usedScenarioTitle ? ` · ${debug.usedScenarioTitle}` : ""}
        </button>
        {open && (
          <div className="mt-1 space-y-0.5 pl-4">
            <div className="flex flex-wrap items-center gap-1.5">
              <Badge tone={debug.action === "block" ? "danger" : esc ? "warn" : "ok"}>
                {debug.action}
              </Badge>
              {esc && <Badge tone="warn">🔴 эскалация</Badge>}
            </div>
            <div>
              RAG:{" "}
              {debug.ragCandidates.length
                ? debug.ragCandidates.map((c) => `#${c.id} · ${c.score.toFixed(3)}`).join("   ")
                : "—"}
            </div>
            {exEntries.length > 0 && (
              <div>
                extracted: {exEntries.map(([k, v]) => `${k}=${String(v)}`).join(", ")}
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function Badge({ tone, children }: { tone: "ok" | "warn" | "danger"; children: ReactNode }) {
  const cls = {
    ok: "bg-success-bg text-success",
    warn: "bg-accent-bg text-accent-ink",
    danger: "bg-danger-bg text-danger",
  }[tone];
  return <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-semibold", cls)}>{children}</span>;
}

function Dot({ d = 0 }: { d?: number }) {
  return (
    <span
      className="inline-block h-1.5 w-1.5 animate-bounce rounded-full bg-on-primary/80"
      style={{ animationDelay: `${d}s` }}
    />
  );
}
