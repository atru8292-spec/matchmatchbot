import type { ReactNode } from "react";
import { TabBar, type TabId } from "./TabBar";

interface Props {
  active: TabId;
  onTabChange: (id: TabId) => void;
  children: ReactNode;
}

// Каркас: прокручиваемая область контента + фиксированный нижний таб-бар.
// Высота 100% (см. index.css html/body/#root), контент скроллится независимо.
export function AppShell({ active, onTabChange, children }: Props) {
  return (
    <div className="mx-auto flex h-full max-w-lg flex-col bg-paper">
      <main className="flex-1 overflow-y-auto overscroll-contain">{children}</main>
      <TabBar active={active} onChange={onTabChange} />
    </div>
  );
}
