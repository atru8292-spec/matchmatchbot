import React from "react";
import ReactDOM from "react-dom/client";
// self-hosted шрифты (не CDN): Golos Text — заголовки, Manrope — тело
import "@fontsource/golos-text/500.css";
import "@fontsource/golos-text/600.css";
import "@fontsource/golos-text/700.css";
import "@fontsource/manrope/400.css";
import "@fontsource/manrope/500.css";
import "@fontsource/manrope/600.css";
import "@/styles/index.css";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "@/lib/query";
import App from "@/App";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
