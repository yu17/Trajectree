import { useState } from "react";

type Tab = "timeline" | "ledger";

export default function App() {
  const [tab, setTab] = useState<Tab>("timeline");
  return (
    <div>
      <div role="tablist">
        <button role="tab" aria-selected={tab === "timeline"} onClick={() => setTab("timeline")}>
          Timeline
        </button>
        <button role="tab" aria-selected={tab === "ledger"} onClick={() => setTab("ledger")}>
          Ledger
        </button>
      </div>
      <div role="tabpanel">{tab === "timeline" ? <p>Timeline</p> : <p>Ledger</p>}</div>
    </div>
  );
}
