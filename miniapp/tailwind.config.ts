import type { Config } from "tailwindcss";

// Цвета/радиусы/тени — через CSS-переменные (см. src/styles/index.css), OKLCH,
// тёмная тема переключается классом .dark на <html>. Значения — из brand_assets/brand-guide.md.
export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        paper: "var(--color-paper)",
        surface: "var(--color-surface)",
        elevated: "var(--color-elevated)",
        ink: "var(--color-ink)",
        muted: "var(--color-muted)",
        line: "var(--color-line)",
        primary: "var(--color-primary)",
        "primary-hover": "var(--color-primary-hover)",
        "on-primary": "var(--color-on-primary)",
        accent: "var(--color-accent)",
        success: "var(--color-success)",
        danger: "var(--color-danger)",
        "success-bg": "var(--color-success-bg)",
        "danger-bg": "var(--color-danger-bg)",
        info: "var(--color-info)",
        "info-bg": "var(--color-info-bg)",
        neutral: "var(--color-neutral)",
        "neutral-bg": "var(--color-neutral-bg)",
        "accent-ink": "var(--color-accent-ink)",
        "accent-bg": "var(--color-accent-bg)",
      },
      fontFamily: {
        display: ['"Golos Text"', "system-ui", "sans-serif"],
        body: ["Manrope", "system-ui", "sans-serif"],
      },
      borderRadius: {
        control: "0.625rem", // 10px — кнопки/инпуты
        card: "0.875rem", // 14px — карточки
      },
      boxShadow: {
        // слоёные тонированные тени (не flat shadow-md)
        soft: "0 1px 2px oklch(0.3 0.02 200 / 0.05), 0 4px 12px oklch(0.3 0.02 200 / 0.06)",
        lift: "0 2px 6px oklch(0.3 0.02 200 / 0.08), 0 12px 32px oklch(0.3 0.02 200 / 0.10)",
      },
      transitionTimingFunction: {
        standard: "cubic-bezier(0.2, 0, 0, 1)",
      },
    },
  },
  plugins: [],
} satisfies Config;
