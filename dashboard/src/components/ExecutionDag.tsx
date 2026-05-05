// Renders the execution DAG for a spec using Mermaid's flowchart.
//
// Mermaid is initialised once on first mount with the dark theme so it
// blends with the existing dashboard styling. Each render call goes
// through `mermaid.render(id, def)` which returns SVG text we inject
// into the container.

import React, { useEffect, useRef, useState } from "react";
import mermaid from "mermaid";

import { buildDagDefinition } from "./dag";
import type { SpecDetailResponse } from "../types/api";

let mermaidInitialised = false;

function initMermaidOnce(): void {
  if (mermaidInitialised) return;
  mermaid.initialize({
    startOnLoad: false,
    theme: "dark",
    securityLevel: "strict",
    flowchart: {
      curve: "basis",
      htmlLabels: true,
      // Vertical density — the DAGs we render are tall not wide.
      nodeSpacing: 30,
      rankSpacing: 40,
    },
  });
  mermaidInitialised = true;
}

interface ExecutionDagProps {
  detail: SpecDetailResponse;
}

const ExecutionDag: React.FC<ExecutionDagProps> = ({ detail }) => {
  const containerRef = useRef<HTMLDivElement>(null);
  const [errorText, setErrorText] = useState<string | null>(null);
  const [showSource, setShowSource] = useState(false);
  const definition = buildDagDefinition(detail);

  useEffect(() => {
    initMermaidOnce();

    let cancelled = false;
    const renderId = `dag-${detail.spec.id}-${Date.now()}`;

    mermaid
      .render(renderId, definition)
      .then(({ svg }) => {
        if (cancelled || !containerRef.current) return;
        containerRef.current.innerHTML = svg;
        setErrorText(null);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const message = err instanceof Error ? err.message : String(err);
        setErrorText(message);
      });

    return () => {
      cancelled = true;
    };
  }, [definition, detail.spec.id]);

  return (
    <div className="execution-dag">
      <div className="execution-dag-header">
        <h3>Execution DAG ({detail.tasks?.length || 0} tasks, {detail.all_gates?.length || 0} gates)</h3>
        <button
          type="button"
          className="execution-dag-toggle"
          onClick={() => setShowSource((s) => !s)}
          aria-pressed={showSource}
        >
          {showSource ? "hide source" : "show source"}
        </button>
      </div>

      {errorText ? (
        <div className="execution-dag-error">
          <strong>DAG render failed:</strong> {errorText}
        </div>
      ) : (
        <div ref={containerRef} className="execution-dag-svg" />
      )}

      {showSource && (
        <pre className="execution-dag-source">
          <code>{definition}</code>
        </pre>
      )}
    </div>
  );
};

export default ExecutionDag;
