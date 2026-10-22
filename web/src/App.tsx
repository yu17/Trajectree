import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "./api/client";
import { ProjectForm } from "./components/ProjectForm";
import { QuickCapture } from "./components/QuickCapture";
import { LedgerView } from "./views/LedgerView";
import { TimelineView } from "./views/TimelineView";
import "./views/views.css";

type Tab = "timeline" | "ledger";
type Panel = "project" | "capture" | null;

export default function App() {
  const [tab, setTab] = useState<Tab>("timeline");
  const [panel, setPanel] = useState<Panel>(null);
  const qc = useQueryClient();
  const projects = useQuery({ queryKey: ["projects"], queryFn: api.projects });

  const created = () => {
    setPanel(null);
    void qc.invalidateQueries({ queryKey: ["timeline"] });
    void qc.invalidateQueries({ queryKey: ["projects"] });
    void qc.invalidateQueries({ queryKey: ["txns"] });
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
        </div>
        <div className="header-actions">
          <button onClick={() => toggle("project")}>+ Project</button>
          <button onClick={() => toggle("capture")}>Quick capture</button>
        </div>
      </header>
      {panel && (
        <aside className="drawer">
          {panel === "project" && <ProjectForm onCreated={created} />}
          {panel === "capture" && <QuickCapture projects={projects.data ?? []} onCreated={created} />}
        </aside>
      )}
      <div role="tabpanel">{tab === "timeline" ? <TimelineView /> : <LedgerView />}</div>
    </div>
  );
}
