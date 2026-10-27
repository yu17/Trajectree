import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "./api/client";
import { AskPanel } from "./components/AskPanel";
import { ProjectForm } from "./components/ProjectForm";
import { QuickCapture } from "./components/QuickCapture";
import { parseRoute, routePath, type Route, type Tab } from "./route";
import { InboxView, inspectionsQuery, isDue, pendingTxnsQuery } from "./views/InboxView";
import { LedgerView } from "./views/LedgerView";
import { ProjectView } from "./views/ProjectView";
import { RunsView } from "./views/RunsView";
import { SettingsView } from "./views/SettingsView";
import { TimelineView } from "./views/TimelineView";
import "./views/views.css";

// Marp (with its MathJax) and CodeMirror are most of the bundle; load them with the Reports tab.
const ReportsView = lazy(() => import("./views/ReportsView").then(m => ({ default: m.ReportsView })));

type Panel = "project" | "capture" | "ask" | null;

const PANEL_LABEL: Record<Exclude<Panel, null>, string> = { project: "New project", capture: "Quick capture", ask: "Ask" };

const isUnauthorized = (err: unknown) => err instanceof ApiError && err.status === 401;

export default function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.pathname));
  const [panel, setPanel] = useState<Panel>(null);
  const qc = useQueryClient();
  const { tab } = route;

  // Back and forward re-read the path; the open project survives a step to another tab.
  useEffect(() => {
    const onPop = () => setRoute(cur => {
      const next = parseRoute(window.location.pathname);
      return next.tab === "project" ? next : { ...next, project: cur.project };
    });
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);
  const go = useCallback((next: Route) => {
    setRoute(next);
    const path = routePath(next);
    if (path !== window.location.pathname) window.history.pushState(null, "", path);
  }, []);
  const showTab = (t: Tab) => go({ tab: t, project: route.project });

  // Escape closes the drawer, which covers the header buttons that open it.
  useEffect(() => {
    if (!panel) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setPanel(null);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [panel]);

  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: api.projects,
    // A 401 will not fix itself on retry; surface the login banner at once.
    retry: (n, err) => !isUnauthorized(err) && n < 3,
  });
  const pending = useQuery({ ...pendingTxnsQuery, retry: (n, err) => !isUnauthorized(err) && n < 3 });
  const inspections = useQuery({ ...inspectionsQuery, retry: (n, err) => !isUnauthorized(err) && n < 3 });
  const pendingCount = (pending.data?.length ?? 0) + (inspections.data?.filter(isDue).length ?? 0);

  // The URL names the project by slug; an id resolves too.
  const openRef = route.project;
  const openProject = openRef ? projects.data?.find(p => p.slug === openRef || p.id === openRef) ?? null : null;
  const projectList = projects.data;
  const onOpenProject = useCallback((id: string) => {
    go({ tab: "project", project: projectList?.find(p => p.id === id)?.slug ?? id });
  }, [go, projectList]);
  const closeProject = () => go({ tab: "timeline", project: null });

  const created = () => {
    setPanel(null);
    void qc.invalidateQueries({ queryKey: ["timeline"] });
    void qc.invalidateQueries({ queryKey: ["projects"] });
    void qc.invalidateQueries({ queryKey: ["txns"] });
    void qc.invalidateQueries({ queryKey: ["todos"] });
  };
  const toggle = (p: Exclude<Panel, null>) => setPanel(cur => (cur === p ? null : p));
  const tabButton = (t: Tab, label: string) => (
    <button role="tab" aria-selected={tab === t} onClick={() => showTab(t)}>{label}</button>
  );

  return (
    <div className="app">
      <header className="app-header">
        <div role="tablist">
          {tabButton("timeline", "Timeline")}
          {tabButton("ledger", "Ledger")}
          {tabButton("runs", "Runs")}
          {tabButton("reports", "Reports")}
          {openRef && tabButton("project", "Project")}
          {tabButton("inbox", pendingCount > 0 ? `Inbox (${pendingCount})` : "Inbox")}
          {tabButton("settings", "Settings")}
        </div>
        <div className="header-actions">
          <button onClick={() => toggle("project")}>+ Project</button>
          <button onClick={() => toggle("capture")}>Quick capture</button>
          <button onClick={() => toggle("ask")}>Ask</button>
        </div>
      </header>
      {isUnauthorized(projects.error) && (
        <p role="alert" className="notice">
          Not logged in. Open the login URL printed by the daemon (for Docker:{" "}
          <code>docker compose logs trajectree | grep ready</code>).
        </p>
      )}
      {panel && (
        <aside className="drawer" aria-label={PANEL_LABEL[panel]}>
          <button className="drawer-close" aria-label="Close" title="Close (Esc)" onClick={() => setPanel(null)}>×</button>
          {panel === "project" && <ProjectForm onCreated={created} />}
          {panel === "capture" && <QuickCapture projects={projects.data ?? []} onCreated={created} />}
          {panel === "ask" && <AskPanel projects={projects.data ?? []} />}
        </aside>
      )}
      <div role="tabpanel">
        {tab === "timeline" ? (
          <TimelineView onOpenProject={onOpenProject} />
        ) : tab === "project" ? (
          openProject ? (
            <ProjectView key={openProject.id} projectId={openProject.id} onClose={closeProject} />
          ) : projects.isPending ? (
            <p className="muted">loading…</p>
          ) : (
            <p className="muted">{`No project "${openRef}"`}</p>
          )
        ) : tab === "ledger" ? (
          <LedgerView />
        ) : tab === "runs" ? (
          <RunsView />
        ) : tab === "reports" ? (
          <Suspense fallback={<p className="muted">Loading</p>}>
            <ReportsView />
          </Suspense>
        ) : tab === "settings" ? (
          <SettingsView />
        ) : (
          <InboxView />
        )}
      </div>
    </div>
  );
}
