import { lazy, Suspense, useCallback, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "./api/client";
import { AskPanel } from "./components/AskPanel";
import { ProjectForm } from "./components/ProjectForm";
import { QuickCapture } from "./components/QuickCapture";
import { InboxView, pendingTxnsQuery } from "./views/InboxView";
import { LedgerView } from "./views/LedgerView";
import { ProjectView } from "./views/ProjectView";
import { RunsView } from "./views/RunsView";
import { TimelineView } from "./views/TimelineView";
import "./views/views.css";

// Marp (with its MathJax) and CodeMirror are most of the bundle; load them with the Reports tab.
const ReportsView = lazy(() => import("./views/ReportsView").then(m => ({ default: m.ReportsView })));

type Tab = "timeline" | "ledger" | "runs" | "reports" | "project" | "inbox";
type Panel = "project" | "capture" | "ask" | null;

const isUnauthorized = (err: unknown) => err instanceof ApiError && err.status === 401;

export default function App() {
  const [tab, setTab] = useState<Tab>("timeline");
  const [panel, setPanel] = useState<Panel>(null);
  const [openProject, setOpenProject] = useState<string | null>(null);
  const qc = useQueryClient();
  const onOpenProject = useCallback((id: string) => {
    setOpenProject(id);
    setTab("project");
  }, []);
  const closeProject = () => {
    setOpenProject(null);
    setTab("timeline");
  };
  const projects = useQuery({
    queryKey: ["projects"],
    queryFn: api.projects,
    // A 401 will not fix itself on retry; surface the login banner at once.
    retry: (n, err) => !isUnauthorized(err) && n < 3,
  });
  const pending = useQuery({ ...pendingTxnsQuery, retry: (n, err) => !isUnauthorized(err) && n < 3 });
  const pendingCount = pending.data?.length ?? 0;

  const created = () => {
    setPanel(null);
    void qc.invalidateQueries({ queryKey: ["timeline"] });
    void qc.invalidateQueries({ queryKey: ["projects"] });
    void qc.invalidateQueries({ queryKey: ["txns"] });
    void qc.invalidateQueries({ queryKey: ["todos"] });
  };
  const toggle = (p: Exclude<Panel, null>) => setPanel(cur => (cur === p ? null : p));

  return (
    <div className="app">
      <header className="app-header">
        <div role="tablist">
          <button role="tab" aria-selected={tab === "timeline"} onClick={() => setTab("timeline")}>
            Timeline
          </button>
          <button role="tab" aria-selected={tab === "ledger"} onClick={() => setTab("ledger")}>
            Ledger
          </button>
          <button role="tab" aria-selected={tab === "runs"} onClick={() => setTab("runs")}>
            Runs
          </button>
          <button role="tab" aria-selected={tab === "reports"} onClick={() => setTab("reports")}>
            Reports
          </button>
          {openProject && (
            <button role="tab" aria-selected={tab === "project"} onClick={() => setTab("project")}>
              Project
            </button>
          )}
          <button role="tab" aria-selected={tab === "inbox"} onClick={() => setTab("inbox")}>
            {pendingCount > 0 ? `Inbox (${pendingCount})` : "Inbox"}
          </button>
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
        <aside className="drawer">
          {panel === "project" && <ProjectForm onCreated={created} />}
          {panel === "capture" && <QuickCapture projects={projects.data ?? []} onCreated={created} />}
          {panel === "ask" && <AskPanel projects={projects.data ?? []} />}
        </aside>
      )}
      <div role="tabpanel">
        {tab === "timeline" ? (
          <TimelineView onOpenProject={onOpenProject} />
        ) : tab === "project" && openProject ? (
          <ProjectView key={openProject} projectId={openProject} onClose={closeProject} />
        ) : tab === "ledger" ? (
          <LedgerView />
        ) : tab === "runs" ? (
          <RunsView />
        ) : tab === "reports" ? (
          <Suspense fallback={<p className="muted">Loading</p>}>
            <ReportsView />
          </Suspense>
        ) : (
          <InboxView />
        )}
      </div>
    </div>
  );
}
