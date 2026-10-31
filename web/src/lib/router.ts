// Minimal hash router: #/ (dashboard) and #/positions/:id. Hash routing means the API's static
// fallback never has to know the client's routes.
import { useSyncExternalStore } from "react";

const subscribe = (cb: () => void) => {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
};
const read = () => window.location.hash.replace(/^#/, "") || "/";

export function useRoute(): string {
  return useSyncExternalStore(subscribe, read, () => "/");
}

export function navigate(path: string) {
  window.location.hash = path;
}
