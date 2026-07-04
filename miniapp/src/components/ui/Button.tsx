import { forwardRef, type ButtonHTMLAttributes, type ReactNode } from "react";
import { cn } from "@/lib/cn";

type Variant = "primary" | "secondary" | "ghost" | "danger";
type Size = "sm" | "md";

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: Size;
  icon?: ReactNode; // Lucide-иконка (line), слева от текста
}

// Все состояния: default/hover/focus-visible/active/disabled. Один primary на экран.
const VARIANT: Record<Variant, string> = {
  primary:
    "bg-primary text-on-primary shadow-soft hover:bg-primary-hover active:brightness-95",
  secondary:
    "bg-surface text-ink border border-line hover:bg-elevated active:brightness-95",
  ghost: "text-muted hover:bg-elevated hover:text-ink active:brightness-95",
  danger: "bg-danger-bg text-danger hover:brightness-95 active:brightness-90",
};

const SIZE: Record<Size, string> = {
  sm: "h-9 px-3 text-sm gap-1.5",
  md: "h-11 px-4 text-[15px] gap-2", // ≥44px touch-target
};

export const Button = forwardRef<HTMLButtonElement, Props>(function Button(
  { variant = "primary", size = "md", icon, className, children, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      className={cn(
        "inline-flex items-center justify-center rounded-control font-medium",
        "transition-[background-color,color,filter] duration-150 ease-standard",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary/40",
        "disabled:pointer-events-none disabled:opacity-50",
        VARIANT[variant],
        SIZE[size],
        className,
      )}
      {...rest}
    >
      {icon}
      {children}
    </button>
  );
});
