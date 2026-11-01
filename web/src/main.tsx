import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import * as Tooltip from "@radix-ui/react-tooltip";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { ToastProvider } from "./components/ui";
import "./styles.css";

// Financially important state is never updated optimistically: every mutation waits for the server.
const queryClient = new QueryClient({
  defaultOptions: { queries: { retry: 1, refetchOnWindowFocus: false } },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <Tooltip.Provider>
        <ToastProvider>
          <App />
        </ToastProvider>
      </Tooltip.Provider>
    </QueryClientProvider>
  </StrictMode>,
);
