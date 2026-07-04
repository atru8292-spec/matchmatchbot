import type { HTMLAttributes } from "react";
import { cn } from "@/lib/cn";

// Поверхность-контейнер: surface-фон, тонкая граница, слоёная тень (не shadow-md).
export function Card({ className, ...rest }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "rounded-card border border-line bg-surface shadow-soft",
        className,
      )}
      {...rest}
    />
  );
}
