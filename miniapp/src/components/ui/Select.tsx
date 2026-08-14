import { forwardRef, type ReactNode, type SelectHTMLAttributes } from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/cn";

interface Props extends SelectHTMLAttributes<HTMLSelectElement> {
  leading?: ReactNode;
}

export const Select = forwardRef<HTMLSelectElement, Props>(function Select(
  { leading, className, children, ...rest },
  ref,
) {
  return (
    <div
      className={cn(
        "flex items-center gap-2 h-11 rounded-control border border-line bg-surface px-3",
        "focus-within:border-primary/60 focus-within:ring-2 focus-within:ring-primary/20",
        "transition-[border-color,box-shadow] duration-150 ease-standard",
        className,
      )}
    >
      {leading && <span className="shrink-0 text-muted">{leading}</span>}
      <select
        ref={ref}
        className="min-w-0 flex-1 appearance-none bg-transparent text-[15px] text-ink outline-none"
        {...rest}
      >
        {children}
      </select>
      <ChevronDown size={16} className="shrink-0 text-muted" />
    </div>
  );
});
