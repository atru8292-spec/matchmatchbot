import { QueryClient } from "@tanstack/react-query";

// Один клиент на приложение. staleTime 15с — мини-CRM про свежесть, но без спама.
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 15_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});
