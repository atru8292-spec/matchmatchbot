import { forwardRef, type InputHTMLAttributes, type ReactNode } from "react";
import { cn } from "@/lib/cn";

interface Props extends InputHTMLAttributes<HTMLInputElement> {
  leading?: ReactNode; // иконка слева (напр. поиск)
  trailing?: ReactNode; // кнопка/иконка справа (напр. очистить)
}

export const Input = forwardRef<HTMLInputElement, Props>(function Input(
  { leading, trailing, className, ...rest },
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
      <input
        ref={ref}
        className={cn(
          "min-w-0 flex-1 bg-transparent text-[15px] text-ink outline-none",
          "placeholder:text-muted",
        )}
        {...rest}
      />
      {trailing && <span className="shrink-0">{trailing}</span>}
    </div>
  );
});
