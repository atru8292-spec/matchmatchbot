import type { ReactNode } from "react";
import { EmptyState } from "@/components/ui/EmptyState";

// Временная заглушка для вкладок, которые появятся в следующих фазах.
export function Placeholder({
  icon,
  title,
  description,
}: {
  icon: ReactNode;
  title: string;
  description: string;
}) {
  return (
    <div className="flex h-full flex-col">
      <header className="border-b border-line px-4 pt-4 pb-3">
        <h1 className="text-xl text-ink">{title}</h1>
      </header>
      <div className="flex flex-1 items-center justify-center">
        <EmptyState icon={icon} title="Скоро" description={description} />
      </div>
    </div>
  );
}
