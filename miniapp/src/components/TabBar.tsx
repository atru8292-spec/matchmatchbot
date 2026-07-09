import { Users, UserCheck, CalendarHeart, BarChart3, FlaskConical } from "lucide-react";
import { cn } from "@/lib/cn";

export type TabId = "leads" | "clients" | "event" | "stats" | "test";

const TABS: { id: TabId; label: string; icon: typeof Users }[] = [
  { id: "leads", label: "Лиды", icon: Users },
  { id: "clients", label: "Клиенты", icon: UserCheck },
  { id: "event", label: "Ивент", icon: CalendarHeart },
  { id: "stats", label: "Статы", icon: BarChart3 },
  { id: "test", label: "Тест", icon: FlaskConical },
];

interface Props {
  active: TabId;
  onChange: (id: TabId) => void;
}

// Нижний таб-бар (mobile-first Telegram). Safe-area снизу, активная — primary.
export function TabBar({ active, onChange }: Props) {
  return (
    <nav
      className="shrink-0 border-t border-line bg-surface/95 backdrop-blur"
      style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
    >
      <ul className="mx-auto flex max-w-lg">
        {TABS.map(({ id, label, icon: Icon }) => {
          const isActive = id === active;
          return (
            <li key={id} className="flex-1">
              <button
                onClick={() => onChange(id)}
                aria-current={isActive ? "page" : undefined}
                className={cn(
                  "flex h-14 w-full flex-col items-center justify-center gap-1",
                  "transition-colors duration-150 ease-standard",
                  isActive ? "text-primary" : "text-muted hover:text-ink",
                )}
              >
                <Icon size={22} strokeWidth={isActive ? 2.4 : 1.8} />
                <span className="text-[11px] font-medium">{label}</span>
              </button>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
