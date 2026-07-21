// Read-only tier-a dashboard. Reads static published aggregates from publish/ output.
// Deliberately has NO backend or write path (smallest attack/maintenance surface).
import React from "react";
import { createRoot } from "react-dom/client";

function App() {
  return (
    <main style={{ fontFamily: "system-ui", padding: 24 }}>
      <h1>Uncorrupt</h1>
      <p>Phase-1 walking skeleton. Renders published tier-a aggregates (coming soon).</p>
    </main>
  );
}

createRoot(document.getElementById("root")!).render(<App />);
