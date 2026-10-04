"use client";

import dynamic from "next/dynamic";
import { useMemo, useState } from "react";
import { calculateProjectCostMetrics, resolveProjectComponentInstances } from "../../lib/project-cost-metrics";
import { projectCadModel, resolveCadModel } from "../../lib/cad-model";
import { resolveProjectImageCandidates } from "../../lib/project-images";
import { buildProjectDocsMarkdown } from "../../lib/docs-export";
import type { ProjectRevisionSnapshot } from "../../lib/project-history";
import { AssemblyPanel, BomPanel, MechanicalPanel, OverviewPanel } from "./project-detail-panels";
import CadModelPanel from "./cad-model-panel";
import { useProjectHistory } from "./project-history";
import styles from "./chat-project-layout.module.css";

const SchematicCanvas = dynamic(() => import("../../components/schematic-canvas"), { ssr: false });

/** Snapshot content owns no live-project mutation callbacks or latest-export requests. */
export default function ProjectRevisionPreview() {
  const history = useProjectHistory();
  if (!history?.selection) return null;
  const { selection } = history;
  if (selection.error) return <div className={styles.historyEmpty} role="alert">
    <p>{selection.error}</p>
    <button className={styles.button} type="button" onClick={() => history.select(selection.id)}>Try again</button>
  </div>;
  if (!selection.snapshot) return <p className={styles.historyEmpty} role="status">Loading saved version…</p>;
  return <SnapshotContent key={selection.id} snapshot={selection.snapshot} apiUrl={history.config.apiUrl} getHeaders={history.config.getHeaders} />;
}

function saveFile(content: string, filename: string, type: string) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const link = document.createElement("a");
  link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export function SnapshotContent({ snapshot, apiUrl, getHeaders, shared = false }: {
  snapshot: ProjectRevisionSnapshot; apiUrl: string; getHeaders: () => Promise<Record<string, string>>; shared?: boolean;
}) {
  const [tab, setTab] = useState("overview");
  const [toggles, setToggles] = useState<Record<string, boolean>>({});
  const [electrical, setElectrical] = useState(false);
  const ir = snapshot.project_ir;
  const components = useMemo(() => resolveProjectComponentInstances(ir), [ir]);
  const metrics = useMemo(() => calculateProjectCostMetrics(ir), [ir]);
  const schematic = useMemo(() => ({ ...ir, components }), [ir, components]);
  const metadata = ir.assembly_metadata || {};
  const images = useMemo(() => resolveProjectImageCandidates(ir.assembly_metadata || {}, false), [ir]);
  const cad = projectCadModel(ir);
  const descriptor = resolveCadModel(cad);
  const hasCad = descriptor && descriptor.kind !== "unsupported";
  const features = metadata.image_features?.length ? metadata.image_features : ir.constraints || [];
  const issues = [...(ir.validation?.critical || []), ...(ir.validation?.warning || []), ...(ir.validation?.info || []), ...(ir.validation_issues || [])];
  const tabs = [
    ["overview", "Overview"], ["mechanical", "Mechanical"], ...(hasCad ? [["cad", "CAD"]] : []),
    ["bom", "BOM"], ["schematic", "Wiring"], ["assembly", "Assembly"], ["exports", "Exports"],
  ];
  const content = (() => {
    switch (tab) {
      case "overview": return <OverviewPanel title={snapshot.title} description={ir.overview?.description || ""} imageCandidates={images}
        features={features} metrics={metrics} metadata={metadata} systemArchitecture={ir.system_architecture} showImageSection={images.length > 0} />;
      case "mechanical": return <MechanicalPanel systemArchitecture={ir.system_architecture} toggles={toggles} setToggles={setToggles} electricalActive={electrical}
        setElectricalActive={setElectrical} components={components} features={features} metadata={metadata} mechanical={ir.mechanical || {}}
        cadModel={cad && typeof cad === "object" ? cad as Record<string, any> : null} />;
      case "cad": return <CadModelPanel cadModel={cad} apiUrl={apiUrl} getHeaders={getHeaders} revisionId={snapshot.revision_id} shared={shared} />;
      case "bom": return <BomPanel components={ir.bom?.length ? ir.bom : components} metrics={metrics}
        cadSources={ir.mechanical?.cad_sources || []} fabricationCost={Number(ir.mechanical?.fabrication_cost_estimate_usd || 0)} canDownloadAssets />;
      case "schematic": return <SchematicCanvas project={schematic} />;
      case "assembly": return <AssemblyPanel assembly={ir.assembly || []} issues={issues} />;
      case "exports": return <div className={styles.historyEmpty}>
        <h3>Downloads for v{snapshot.revision}</h3>
        <div className={styles.snapshotActions}>
          <button type="button" className={styles.button} onClick={() => saveFile(JSON.stringify(ir, null, 2), `project-v${snapshot.revision}.json`, "application/json")}>Project JSON</button>
          <button type="button" className={styles.button} onClick={() => saveFile(buildProjectDocsMarkdown({
            title: snapshot.title, description: ir.overview?.description, assembly: ir.assembly || [], issues,
          }), `project-v${snapshot.revision}.md`, "text/markdown;charset=utf-8")}>Build documentation</button>
          {hasCad && <button type="button" className={styles.button} onClick={() => setTab("cad")}>Open saved CAD</button>}
        </div>
        {!shared && <p>Return to latest to create new mesh exports or G-code.</p>}
      </div>;
      default: return null;
    }
  })();
  return <div className={styles.surface} data-testid="revision-preview" data-revision={snapshot.revision}>
    <nav className={styles.snapshotNav} aria-label="Saved version views">{tabs.map(([id, label]) => <button
      key={id} type="button" className={styles.button} aria-pressed={tab === id} onClick={() => setTab(id)}>{label}</button>)}</nav>
    <div className={styles.surfaceContent}>{content}</div>
  </div>;
}
