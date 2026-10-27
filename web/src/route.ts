// Each tab has its own path, so a refresh or a bookmark lands on it; the daemon serves
// index.html for any path that is not an asset, /api or /mcp.

export type Tab = "timeline" | "ledger" | "runs" | "reports" | "project" | "inbox" | "settings";

// `project` is the open project's slug (or id): kept while other tabs are shown, so the
// Project tab stays available.
export interface Route {
  tab: Tab;
  project: string | null;
}

const TABS: Tab[] = ["ledger", "runs", "reports", "inbox", "settings"];

export function parseRoute(pathname: string): Route {
  const parts = pathname.split("/").filter(Boolean).map(decodeURIComponent);
  if (parts[0] === "project" && parts[1]) return { tab: "project", project: parts[1] };
  const tab = TABS.find(t => t === parts[0]);
  return { tab: tab ?? "timeline", project: null };
}

export function routePath(route: Route): string {
  if (route.tab === "project" && route.project) return `/project/${encodeURIComponent(route.project)}`;
  return route.tab === "timeline" || route.tab === "project" ? "/" : `/${route.tab}`;
}
