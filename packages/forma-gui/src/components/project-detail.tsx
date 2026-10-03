"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import type { FormaArtifact, FormaHardwareProject, FormaProjectResponse, FormaValidationFinding } from "../contracts";
import { FormaApiClient, FormaApiError } from "../api";

export type FormaProjectDetailSection = "overview" | "bom" | "validation" | "schematic" | "mechanical" | "assembly" | "artifacts";

export type FormaProjectDetailProps = {
  client?: FormaApiClient;
  projectId?: string;
  /** A response from `FormaApiClient.getProject`, useful for server-loaded or controlled views. */
  project?: FormaProjectResponse;
  initialSection?: FormaProjectDetailSection;
  activeSection?: FormaProjectDetailSection;
  onSectionChange?: (section: FormaProjectDetailSection) => void;
  onBack?: () => void;
  renderSection?: (section: FormaProjectDetailSection, project: FormaProjectResponse) => ReactNode;
  loading?: boolean;
  error?: unknown;
  className?: string;
};

const sections: Array<{ id: FormaProjectDetailSection; label: string }> = [
  { id: "overview", label: "Overview" },
  { id: "bom", label: "BOM" },
  { id: "validation", label: "Validation" },
  { id: "schematic", label: "Schematic" },
  { id: "mechanical", label: "Mechanical" },
  { id: "assembly", label: "Assembly" },
  { id: "artifacts", label: "Artifacts" },
];

function asText(value: unknown, fallback = "") {
  return typeof value === "string" && value.trim() ? value.trim() : fallback;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function asNumber(value: unknown, fallback = 0) {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
}

function errorLabel(error: unknown) {
  if (error instanceof FormaApiError && error.unauthorized) return "Sign-in or project access is required.";
  if (error instanceof Error && error.message) return error.message;
  return "The project could not be loaded. Check the Forma API and try again.";
}

function projectData(response: FormaProjectResponse) {
  return response.project_ir || asRecord(response.project_object).project_ir as FormaHardwareProject || {};
}

function projectImage(response: FormaProjectResponse, ir: FormaHardwareProject) {
  const metadata = asRecord(ir.assembly_metadata);
  const direct = metadata.product_image_url || metadata.image_output_url;
  if (typeof direct === "string" && direct.trim()) return direct;
  const data = metadata.product_image_data;
  if (typeof data === "string" && data.trim()) return data.startsWith("data:") ? data : `data:${asText(metadata.product_image_content_type, "image/png")};base64,${data}`;
  return null;
}

function validationFindings(ir: FormaHardwareProject): FormaValidationFinding[] {
  const validation = ir.validation || {};
  return [
    ...(validation.critical || []),
    ...(validation.warning || []),
    ...(validation.info || []),
    ...(ir.validation_issues || []),
  ];
}

function projectArtifacts(response: FormaProjectResponse, ir: FormaHardwareProject): FormaArtifact[] {
  const mechanical = ir.mechanical || {};
  return [...(response.artifacts || []), ...(mechanical.cad_sources || [])].filter((artifact, index, all) => {
    const url = asText(artifact.url || artifact.href || artifact.file_url);
    return Boolean(url) && all.findIndex((candidate) => asText(candidate.url || candidate.href || candidate.file_url) === url) === index;
  });
}

export function FormaProjectDetail({
  client,
  projectId,
  project,
  initialSection = "overview",
  activeSection,
  onSectionChange,
  onBack,
  renderSection,
  loading: controlledLoading,
  error: controlledError,
  className = "",
}: FormaProjectDetailProps) {
  const [loadedProject, setLoadedProject] = useState<FormaProjectResponse | null>(project || null);
  const [localLoading, setLocalLoading] = useState(!project && Boolean(projectId && client));
  const [localError, setLocalError] = useState<unknown>(null);
  const [localSection, setLocalSection] = useState<FormaProjectDetailSection>(initialSection);
  const currentProject = project || loadedProject;
  const section = activeSection || localSection;

  useEffect(() => {
    if (project) {
      setLoadedProject(project);
      setLocalLoading(false);
      setLocalError(null);
      return;
    }
    if (!client || !projectId) {
      setLocalLoading(false);
      return;
    }
    let active = true;
    setLocalLoading(true);
    setLocalError(null);
    void client.getProject(projectId)
      .then((response) => { if (active) setLoadedProject(response); })
      .catch((error: unknown) => { if (active) setLocalError(error); })
      .finally(() => { if (active) setLocalLoading(false); });
    return () => { active = false; };
  }, [client, project, projectId]);

  const loading = controlledLoading ?? localLoading;
  const error = controlledError ?? localError ?? (!currentProject && !client ? new Error("Configure a FormaApiClient or pass a project response.") : null);
  const setSection = (next: FormaProjectDetailSection) => {
    if (onSectionChange) onSectionChange(next);
    else setLocalSection(next);
  };

  if (loading) return <div className={`forma-gui forma-gui-detail ${className}`} aria-busy="true"><div className="forma-gui-state"><strong>Loading project</strong><span>Fetching the canonical project package from Forma.</span></div></div>;
  if (error) return <div className={`forma-gui forma-gui-detail ${className}`}><div className="forma-gui-state forma-gui-state-error" role="alert"><strong>{error instanceof FormaApiError && error.unauthorized ? "Project access required" : "Project unavailable"}</strong><span>{errorLabel(error)}</span>{onBack && <button type="button" onClick={onBack}>Back to projects</button>}</div></div>;
  if (!currentProject) return null;

  const ir = projectData(currentProject);
  const title = asText(ir.overview?.title, asText(currentProject.title, "Untitled hardware project"));
  const content = renderSection ? renderSection(section, currentProject) : <DefaultSection section={section} response={currentProject} ir={ir} />;

  return (
    <section className={`forma-gui forma-gui-detail ${className}`}>
      <header className="forma-gui-detail-header">
        {onBack && <button type="button" className="forma-gui-back" onClick={onBack}>← Projects</button>}
        <div><span className="forma-gui-kicker">Forma project</span><h1>{title}</h1></div>
      </header>
      <nav className="forma-gui-tabs" aria-label="Project sections">
        {sections.map((item) => <button key={item.id} type="button" onClick={() => setSection(item.id)} aria-current={section === item.id ? "page" : undefined}>{item.label}</button>)}
      </nav>
      <div className="forma-gui-detail-content">{content}</div>
    </section>
  );
}

function DefaultSection({ section, response, ir }: { section: FormaProjectDetailSection; response: FormaProjectResponse; ir: FormaHardwareProject }) {
  switch (section) {
    case "bom": return <BomSection ir={ir} />;
    case "validation": return <ValidationSection ir={ir} />;
    case "schematic": return <SchematicSection response={response} />;
    case "mechanical": return <MechanicalSection ir={ir} />;
    case "assembly": return <AssemblySection ir={ir} />;
    case "artifacts": return <ArtifactsSection artifacts={projectArtifacts(response, ir)} />;
    default: return <OverviewSection response={response} ir={ir} />;
  }
}

function OverviewSection({ response, ir }: { response: FormaProjectResponse; ir: FormaHardwareProject }) {
  const metadata = asRecord(ir.assembly_metadata);
  const components = ir.components || [];
  const bom = ir.bom?.length ? ir.bom : components;
  const findings = validationFindings(ir);
  const totalCost = bom.reduce((sum, item) => sum + asNumber(item.extended_price, asNumber(item.unit_price) * Math.max(1, asNumber(item.quantity, 1))), 0);
  const image = projectImage(response, ir);
  const features = ir.overview?.features || [];
  const workflow = asText(metadata.workflow);
  const status = asText(response.generation_status, asText(response.status, asText(response.readiness, "ready"))).replace(/_/g, " ");
  return <div className="forma-gui-stack">
    {image && <img className="forma-gui-hero" src={image} alt={`${asText(ir.overview?.title, "Project")} concept`} />}
    <div className="forma-gui-panel"><span className="forma-gui-kicker">Technical description</span><p className="forma-gui-lead">{asText(ir.overview?.description, asText(response.prompt, "No project description is available."))}</p>{features.length > 0 && <div className="forma-gui-tags">{features.slice(0, 16).map((feature) => <span key={feature}>{feature}</span>)}</div>}</div>
    <div className="forma-gui-stat-grid"><Stat label="Parts" value={String(components.length || bom.length)} /><Stat label="Estimated cost" value={`$${totalCost.toFixed(2)}`} /><Stat label="Validation" value={findings.length ? `${findings.length} findings` : "Passed"} /><Stat label="Status" value={status} /></div>
    {workflow && <div className="forma-gui-note">Workflow: {workflow}</div>}
  </div>;
}

function BomSection({ ir }: { ir: FormaHardwareProject }) {
  const items = ir.bom?.length ? ir.bom : ir.components || [];
  return <div className="forma-gui-panel"><div className="forma-gui-section-heading"><div><span className="forma-gui-kicker">Bill of materials</span><h2>{items.length} line items</h2></div></div>{items.length ? <div className="forma-gui-table-wrap"><table className="forma-gui-table"><thead><tr><th>Part</th><th>Category</th><th>Qty</th><th>Unit</th><th>Total</th></tr></thead><tbody>{items.map((item, index) => { const quantity = Math.max(1, asNumber(item.quantity, 1)); const unit = asNumber(item.unit_price); const total = asNumber(item.extended_price, unit * quantity); const key = asText(item.line_id, asText(item.part_number, asText(item.ref_des))) || String(index); return <tr key={key}><td><strong>{asText(item.name, asText(item.part_number, "Unnamed part"))}</strong><small>{asText(item.rationale)}</small></td><td>{asText(item.category, "Part")}</td><td>{quantity}</td><td>${unit.toFixed(2)}</td><td>${total.toFixed(2)}</td></tr>; })}</tbody></table></div> : <Empty text="No bill of materials is attached to this project." />}</div>;
}

function ValidationSection({ ir }: { ir: FormaHardwareProject }) {
  const findings = validationFindings(ir);
  return <div className="forma-gui-panel"><div className="forma-gui-section-heading"><div><span className="forma-gui-kicker">Electrical and safety review</span><h2>{findings.length ? `${findings.length} findings` : "Circuit approved"}</h2></div></div>{findings.length ? <div className="forma-gui-findings">{findings.map((finding, index) => <article key={`${finding.code || finding.description || finding.message}-${index}`} className={`forma-gui-finding forma-gui-finding-${asText(finding.severity, "info").toLowerCase()}`}><span>{asText(finding.severity, "info")}</span><p>{asText(finding.description, asText(finding.message, "Validation finding"))}</p>{finding.category && <small>{asText(finding.category)}</small>}</article>)}</div> : <div className="forma-gui-success">All electrical nets validated safely.</div>}</div>;
}

function SchematicSection({ response }: { response: FormaProjectResponse }) {
  if (response.svg_schematic) {
    return <div className="forma-gui-panel"><span className="forma-gui-kicker">Electrical schematic</span><img className="forma-gui-schematic" src={`data:image/svg+xml;charset=utf-8,${encodeURIComponent(response.svg_schematic)}`} alt="Project electrical schematic" /></div>;
  }
  return <div className="forma-gui-panel"><span className="forma-gui-kicker">Electrical schematic</span>{response.mermaid_code ? <pre className="forma-gui-code">{response.mermaid_code}</pre> : <Empty text="No schematic artifact is attached to this project." />}</div>;
}

function MechanicalSection({ ir }: { ir: FormaHardwareProject }) {
  const mechanical = ir.mechanical || {};
  const dimensions = mechanical.render_dimensions || mechanical.external_dimensions_mm;
  const placements = mechanical.component_placements || [];
  return <div className="forma-gui-stack"><div className="forma-gui-panel"><span className="forma-gui-kicker">Mechanical package</span><h2>Enclosure and placement</h2>{dimensions && <div className="forma-gui-dimensions">{Object.entries(dimensions).slice(0, 6).map(([key, value]) => <Stat key={key} label={key.replace(/_mm$/, "")} value={String(value)} />)}</div>}<p className="forma-gui-muted">{placements.length ? `${placements.length} component placements defined.` : "No component placements are attached to this project."}</p></div><ArtifactsSection artifacts={mechanical.cad_sources || []} /></div>;
}

function AssemblySection({ ir }: { ir: FormaHardwareProject }) {
  const steps = ir.assembly || [];
  return <div className="forma-gui-panel"><span className="forma-gui-kicker">Build documentation</span><h2>Assembly sequence</h2>{steps.length ? <div className="forma-gui-steps">{steps.map((step, index) => <article key={`${step.step_num || index}-${step.title}`}><strong>{step.step_num || index + 1}</strong><div><h3>{asText(step.title, "Assembly step")}</h3><p>{asText(step.description, "Follow the project notes for this step.")}</p>{step.danger_flag && <div className="forma-gui-warning">{asText(step.danger_message, "Review safety constraints before continuing.")}</div>}</div></article>)}</div> : <Empty text="No assembly instructions are attached to this project." />}</div>;
}

function ArtifactsSection({ artifacts }: { artifacts: FormaArtifact[] }) {
  return <div className="forma-gui-panel"><span className="forma-gui-kicker">Project files</span><h2>Artifacts</h2>{artifacts.length ? <div className="forma-gui-artifacts">{artifacts.map((artifact, index) => { const href = asText(artifact.url || artifact.href || artifact.file_url); return <a key={`${href}-${index}`} href={href} target="_blank" rel="noreferrer"><span>{asText(artifact.label, asText(artifact.name, "Project artifact"))}</span><small>{asText(artifact.format, asText(artifact.type, "Open file"))}</small></a>; })}</div> : <Empty text="No downloadable artifacts are attached to this project." />}</div>;
}

function Stat({ label, value }: { label: string; value: string }) { return <div className="forma-gui-stat"><span>{label}</span><strong>{value}</strong></div>; }
function Empty({ text }: { text: string }) { return <div className="forma-gui-empty">{text}</div>; }
