import { useState } from "react";
import { StickyNote, Plus } from "lucide-react";
import { Button } from "@/components/ui/Button";

// Композер внутренней заметки (лиду не уходит). Свёрнут до «＋ Заметка», по тапу —
// поле ввода. Сохранение → мутация POST /notes, заметка появляется в таймлайне.
export function NoteComposer({ onAdd, saving }: { onAdd: (text: string) => void; saving?: boolean }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");

  const save = () => {
    const t = text.trim();
    if (!t || saving) return;
    onAdd(t);
    setText("");
    setOpen(false);
  };

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="flex w-full items-center justify-center gap-2 rounded-control border border-dashed border-line py-2.5 text-sm font-medium text-muted transition-colors duration-150 ease-standard hover:border-accent/50 hover:text-accent-ink"
      >
        <Plus size={16} /> Добавить заметку
      </button>
    );
  }

  return (
    <div className="rounded-card border border-accent/40 bg-accent-bg/40 p-2.5">
      <div className="mb-2 flex items-center gap-1 text-[11px] font-semibold text-accent-ink">
        <StickyNote size={12} /> Внутренняя заметка · видит только команда
      </div>
      <textarea
        autoFocus
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={3}
        placeholder="Добавить заметку…"
        className="w-full resize-none rounded-control border border-line bg-surface px-3 py-2 text-[15px] text-ink outline-none placeholder:text-muted focus:border-primary/60 focus:ring-2 focus:ring-primary/20"
      />
      <div className="mt-2 flex justify-end gap-2">
        <Button variant="ghost" size="sm" onClick={() => { setOpen(false); setText(""); }}>
          Отмена
        </Button>
        <Button variant="primary" size="sm" onClick={save} disabled={!text.trim() || saving}>
          {saving ? "Сохранение…" : "Сохранить"}
        </Button>
      </div>
    </div>
  );
}
