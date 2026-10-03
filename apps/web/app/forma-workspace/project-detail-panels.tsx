"use client";

import { useEffect, useRef, useState } from "react";
import dynamic from "next/dynamic";
import Image from "next/image";
import {
  AlertTriangle,
  Battery,
  Box,
  CheckCircle,
  Cpu,
  Database,
  ExternalLink,
  GitBranch,
  Monitor,
  Printer,
  ShieldCheck,
  Sliders,
  Volume2,
  Wrench,
} from "lucide-react";

import { isHardwareReferenceCandidate, type ProjectImageCandidate } from "../../lib/project-images";

const RUNPOD_PARTI_BASE_MODEL = "caid-technologies/parti-base";
const BASETEN_GLM_MODEL = "zai-org/GLM-5.2";

const MechanicalScene = dynamic(() => import("../../components/mechanical-scene"), {
  ssr: false,
  loading: () => (
    <div className="flex h-full min-h-[420px] items-center justify-center bg-[var(--forma-page)] text-xs font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">
      Loading mechanical preview...
    </div>
  ),
});

const categoryTone: Record<string, { text: string; bg: string; border: string; label: string }> = {
  microcontroller: { text: "text-[rgb(var(--forma-cyan-rgb))]", bg: "bg-[rgb(var(--forma-cyan-rgb)/0.1)]", border: "border-[rgb(var(--forma-cyan-rgb)/0.35)]", label: "MCU" },
  sensor: { text: "text-[rgb(var(--forma-green-rgb))]", bg: "bg-[rgb(var(--forma-green-rgb)/0.1)]", border: "border-[rgb(var(--forma-green-rgb)/0.35)]", label: "SENSOR" },
  actuator: { text: "text-[rgb(var(--forma-yellow-rgb))]", bg: "bg-[rgb(var(--forma-yellow-rgb)/0.1)]", border: "border-[rgb(var(--forma-yellow-rgb)/0.35)]", label: "ACTUATOR" },
  display: { text: "text-[rgb(var(--forma-violet-rgb))]", bg: "bg-[rgb(var(--forma-violet-rgb)/0.1)]", border: "border-[rgb(var(--forma-violet-rgb)/0.35)]", label: "DISPLAY" },
  power: { text: "text-[rgb(var(--forma-yellow-rgb))]", bg: "bg-[rgb(var(--forma-yellow-rgb)/0.1)]", border: "border-[rgb(var(--forma-yellow-rgb)/0.35)]", label: "POWER" },
  passives: { text: "text-[rgb(var(--forma-violet-rgb))]", bg: "bg-[rgb(var(--forma-violet-rgb)/0.1)]", border: "border-[rgb(var(--forma-violet-rgb)/0.35)]", label: "IO" },
  mechanical: { text: "text-[rgb(var(--forma-red-rgb))]", bg: "bg-[rgb(var(--forma-red-rgb)/0.1)]", border: "border-[rgb(var(--forma-red-rgb)/0.35)]", label: "MECH" },
  "3d print": { text: "text-[rgb(var(--forma-cyan-rgb))]", bg: "bg-[rgb(var(--forma-cyan-rgb)/0.1)]", border: "border-[rgb(var(--forma-cyan-rgb)/0.35)]", label: "3D PRINT" },
  default: { text: "text-[var(--forma-text-muted)]", bg: "bg-[var(--forma-surface-muted)]", border: "border-[var(--forma-border)]", label: "PART" },
};

function projectLlmDisplayLabel(provider: string, model: string) {
  if (provider === "runpod-serverless" && model === RUNPOD_PARTI_BASE_MODEL) return RUNPOD_PARTI_BASE_MODEL;
  if (provider === "baseten" && model === BASETEN_GLM_MODEL) return "GLM 5.2";
  return `${provider}/${model}`;
}

export function OverviewPanel({
  title,
  description,
  imageCandidates,
  features,
  metrics,
  metadata,
  systemArchitecture,
  showModelName = false,
  showImageSection = true,
  canManageProgressiveReview = false,
  visualDecisionBusy = null,
  visualDecisionError = null,
  onVisualDecision,
}: {
  title: string;
  description: string;
  imageCandidates: ProjectImageCandidate[];
  features: string[];
  metrics: ReturnType<typeof emptyMetrics>;
  metadata: Record<string, any>;
  systemArchitecture?: Record<string, any> | null;
  showModelName?: boolean;
  showImageSection?: boolean;
  canManageProgressiveReview?: boolean;
  visualDecisionBusy?: "approve" | "revise" | "continue_to_cad" | null;
  visualDecisionError?: string | null;
  onVisualDecision?: (decision: "approve" | "revise" | "continue_to_cad", feedback?: string) => void | Promise<void>;
}) {
  const imageKey = imageCandidates.map((candidate) => candidate.src).join("|");
  const [imageIndex, setImageIndex] = useState(0);
  const [revisionFeedback, setRevisionFeedback] = useState("");

  useEffect(() => {
    setImageIndex(0);
  }, [imageKey]);

  const visualGate = metadata?.design_lifecycle?.visual_gate || {};
  const visualArtifactId = String(visualGate.visual_artifact_id || metadata.system_render_artifact_id || "");
  const visualApprovalStatus = String(visualGate.status || metadata.visual_approval_status || "not_requested");
  const persistedVisualFeedback = String(visualGate.feedback || metadata.visual_approval_feedback || "");
  const progressiveReviewVisible = metadata.generation_mode === "progressive" && Boolean(visualArtifactId);
  const cadStatus = String(metadata?.cad_generation?.status || "");
  const cadComplete = cadStatus === "succeeded" || cadStatus === "provided";

  useEffect(() => {
    setRevisionFeedback(persistedVisualFeedback);
  }, [persistedVisualFeedback, visualArtifactId]);

  const productImages = imageCandidates.filter((candidate) => !isHardwareReferenceCandidate(candidate));
  const referenceImages = imageCandidates.filter(isHardwareReferenceCandidate);
  const heroImages = productImages.length ? productImages : referenceImages;
  const activeImage = heroImages[imageIndex] || null;
  const llmProvider = metadata.runtime_provider || metadata.llm_provider || metadata.requested_provider;
  const llmModel = metadata.runtime_model || metadata.actual_model || metadata.model_name || metadata.requested_model;
  const showProductImage = showImageSection || Boolean(activeImage);
  const showHardwareReference = productImages.length > 0 && referenceImages.length > 0;

  return (
    <div className="h-full min-w-0 overflow-y-auto overflow-x-hidden bg-[var(--forma-page)] px-4 py-5 text-[var(--forma-text)] sm:px-5 sm:py-6">
      <div className="mx-auto min-w-0 max-w-[890px]">
        {showProductImage && (
          <div className="relative overflow-hidden rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface-muted)]">
            {activeImage ? (
              <Image
                src={activeImage.src}
                alt={activeImage.label}
                width={1}
                height={1}
                unoptimized
                onError={() => setImageIndex((current) => current + 1)}
                className="h-[280px] w-full object-cover object-center sm:h-[380px]"
              />
            ) : (
              <ImageUnavailableState failed={metadata.image_output_failed === true || metadata.image_output_status === "failed"} />
            )}
            {activeImage && (
              <div className="absolute left-3 top-3 max-w-[calc(100%-1.5rem)] rounded-md border border-[var(--forma-border)] bg-[rgb(var(--forma-chrome-rgb)/0.92)] px-2.5 py-1.5 text-[11px] font-medium text-[var(--forma-text-strong)] shadow-sm backdrop-blur-sm">
                {activeImage.label}
              </div>
            )}
          </div>
        )}

        {showProductImage && heroImages.length > 1 && (
          <div className="mt-3 grid gap-2 sm:grid-cols-3">
            {heroImages.slice(0, 3).map((candidate, index) => (
              <button
                key={`${candidate.src}-${index}`}
                type="button"
                onClick={() => setImageIndex(index)}
                className={`min-w-0 rounded-lg border p-2 text-left transition ${
                  imageIndex === index
                    ? "border-[var(--forma-text-strong)] bg-[var(--forma-surface)] text-[var(--forma-text-strong)]"
                    : "border-[var(--forma-border)] bg-[var(--forma-surface)] text-[var(--forma-text-muted)] hover:border-[var(--forma-text-muted)] hover:text-[var(--forma-text-strong)]"
                }`}
              >
                <Image
                  src={candidate.src}
                  alt={candidate.label}
                  width={1}
                  height={1}
                  unoptimized
                  className="h-20 w-full rounded-md bg-[var(--forma-surface-muted)] object-cover"
                />
                <div className="mt-2 truncate text-[10px] font-medium">{candidate.label}</div>
              </button>
            ))}
          </div>
        )}

        {progressiveReviewVisible && (
          <section data-testid="progressive-visual-review" className="mt-4 rounded-xl border border-[rgb(var(--forma-cyan-rgb)/0.32)] bg-[rgb(var(--forma-cyan-rgb)/0.06)] p-4 sm:p-5">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div className="min-w-0">
                <div className="text-[11px] font-semibold uppercase tracking-[0.16em] text-[rgb(var(--forma-cyan-rgb))]">Progressive concept review</div>
                <h2 className="mt-2 text-sm font-semibold text-[var(--forma-text-strong)]">
                  {visualApprovalStatus === "approved"
                    ? cadComplete ? "Concept approved and CAD is ready" : "Concept approved"
                    : visualApprovalStatus === "rejected"
                      ? "Revision requested"
                      : "Review the whole-system concept before CAD"}
                </h2>
                <p className="mt-2 max-w-2xl text-xs leading-5 text-[var(--forma-text-secondary)]">
                  {visualApprovalStatus === "approved"
                    ? cadComplete
                      ? "The approved concept has continued through the CAD stage."
                      : "The visual is accepted. Continue when you are ready to spend the higher-cost CAD step."
                    : visualApprovalStatus === "rejected"
                      ? "Your feedback is saved. Describe the revision in chat to regenerate only the affected Progressive artifacts."
                      : "Approve the concept, or record what should change before generating component and assembly CAD."}
                </p>
              </div>
              <span className="shrink-0 rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface)] px-2.5 py-1 text-[10px] font-medium uppercase tracking-[0.12em] text-[var(--forma-text-muted)]">
                {visualApprovalStatus.replaceAll("_", " ")}
              </span>
            </div>

            {!cadComplete && visualApprovalStatus !== "approved" && (
              <div className="mt-4">
                <label htmlFor="progressive-revision-feedback" className="text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">
                  Revision feedback
                </label>
                <textarea
                  id="progressive-revision-feedback"
                  value={revisionFeedback}
                  onChange={(event) => setRevisionFeedback(event.target.value)}
                  disabled={Boolean(visualDecisionBusy) || !canManageProgressiveReview}
                  placeholder="Example: make the enclosure lower, move the power connector to the rear, and keep the drivetrain unchanged."
                  className="mt-2 min-h-[82px] w-full resize-y rounded-lg border border-[var(--forma-border)] bg-[var(--forma-page)] px-3 py-2.5 text-xs leading-5 text-[var(--forma-text-body)] outline-none transition focus:border-[rgb(var(--forma-cyan-rgb)/0.6)] disabled:cursor-not-allowed disabled:opacity-60"
                />
              </div>
            )}

            {persistedVisualFeedback && visualApprovalStatus === "rejected" && (
              <div className="mt-3 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface)] px-3 py-2.5 text-xs leading-5 text-[var(--forma-text-secondary)]">
                <span className="font-semibold text-[var(--forma-text-strong)]">Saved feedback:</span> {persistedVisualFeedback}
              </div>
            )}

            {visualDecisionError && (
              <div role="alert" className="mt-3 rounded-lg border border-[rgb(var(--forma-red-rgb)/0.35)] bg-[rgb(var(--forma-red-rgb)/0.08)] px-3 py-2 text-xs text-[rgb(var(--forma-red-rgb))]">
                {visualDecisionError}
              </div>
            )}

            <div className="mt-4 flex flex-wrap gap-2">
              {visualApprovalStatus !== "approved" && (
                <button
                  type="button"
                  onClick={() => void onVisualDecision?.("approve")}
                  disabled={!canManageProgressiveReview || Boolean(visualDecisionBusy)}
                  className="inline-flex items-center gap-2 rounded-md bg-[rgb(var(--forma-cyan-rgb))] px-3 py-2 text-xs font-semibold text-black transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <CheckCircle className="h-4 w-4" />
                  {visualDecisionBusy === "approve" ? "Approving…" : "Approve concept"}
                </button>
              )}
              {visualApprovalStatus !== "approved" && (
                <button
                  type="button"
                  onClick={() => void onVisualDecision?.("revise", revisionFeedback.trim())}
                  disabled={!canManageProgressiveReview || Boolean(visualDecisionBusy) || !revisionFeedback.trim()}
                  className="inline-flex items-center gap-2 rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface)] px-3 py-2 text-xs font-semibold text-[var(--forma-text-strong)] transition hover:border-[var(--forma-text-muted)] disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Sliders className="h-4 w-4" />
                  {visualDecisionBusy === "revise" ? "Saving…" : "Revise"}
                </button>
              )}
              {visualApprovalStatus === "approved" && !cadComplete && (
                <button
                  type="button"
                  onClick={() => void onVisualDecision?.("continue_to_cad")}
                  disabled={!canManageProgressiveReview || Boolean(visualDecisionBusy)}
                  className="inline-flex items-center gap-2 rounded-md bg-[var(--forma-text-strong)] px-3 py-2 text-xs font-semibold text-[var(--forma-page)] transition hover:opacity-90 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  <Wrench className="h-4 w-4" />
                  {visualDecisionBusy === "continue_to_cad" ? "Generating CAD…" : "Continue to CAD"}
                </button>
              )}
            </div>
          </section>
        )}

        {showHardwareReference && (
          <section className="mt-4 rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-3 sm:p-4">
            <div className="text-[11px] font-medium uppercase tracking-[0.16em] text-[var(--forma-text-muted)]">Hardware reference</div>
            <p className="mt-1 text-xs leading-5 text-[var(--forma-text-secondary)]">The image you shared for this project.</p>
            <div className={`mt-3 grid gap-2 ${referenceImages.length > 1 ? "sm:grid-cols-2" : ""}`}>
              {referenceImages.map((candidate) => (
                <Image
                  key={candidate.src}
                  src={candidate.src}
                  alt={candidate.label}
                  width={1}
                  height={1}
                  unoptimized
                  className="h-44 w-full rounded-lg bg-[var(--forma-surface-muted)] object-cover object-center sm:h-52"
                />
              ))}
            </div>
          </section>
        )}

        <div className={`min-w-0 ${showProductImage ? "mt-6 border-t border-[var(--forma-border)] pt-5 sm:pt-6" : ""}`}>
          <h1 className="break-words text-xl font-semibold tracking-tight text-[var(--forma-text-strong)] sm:text-2xl">{title}</h1>
          <div className="mt-4 flex flex-wrap gap-2">
            {metadata.workflow && (
              <span className="max-w-full break-words rounded-md border border-[rgb(var(--forma-cyan-rgb)/0.35)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] px-2.5 py-1 text-[11px] font-medium uppercase tracking-[0.12em] text-[rgb(var(--forma-cyan-rgb))]">
                {metadata.workflow}
              </span>
            )}
            {showModelName && llmProvider && llmModel && (
              <span className="max-w-full break-words rounded-md border border-[rgb(var(--forma-violet-rgb)/0.35)] bg-[rgb(var(--forma-violet-rgb)/0.1)] px-2.5 py-1 text-[11px] font-medium uppercase tracking-[0.12em] text-[rgb(var(--forma-violet-rgb))]">
                {projectLlmDisplayLabel(llmProvider, llmModel)}
              </span>
            )}
            {features.slice(0, 12).map((feature, index) => (
              <span key={`${feature}-${index}`} className="max-w-full break-words rounded-md border border-[var(--forma-border)] px-2.5 py-1 text-[11px] font-medium text-[var(--forma-text-muted)]">
                {String(feature).split(":")[0]}
              </span>
            ))}
          </div>

          <div className="mt-6">
            <div className="text-[11px] font-medium uppercase tracking-[0.16em] text-[var(--forma-text-muted)]">Technical Description</div>
            <p className="mt-3 max-w-3xl break-words text-sm leading-7 text-[var(--forma-text-body)]">{description}</p>
          </div>

          {systemArchitecture?.root && (
            <section className="mt-6 rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4 sm:p-5">
              <div className="flex items-start gap-3 border-b border-[var(--forma-border)] pb-4">
                <span className="flex h-9 w-9 shrink-0 items-center justify-center rounded-md border border-[rgb(var(--forma-cyan-rgb)/0.35)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] text-[rgb(var(--forma-cyan-rgb))]">
                  <GitBranch className="h-4 w-4" />
                </span>
                <div className="min-w-0">
                  <h2 className="text-xs font-semibold uppercase tracking-[0.14em] text-[var(--forma-text-strong)]">System Architecture</h2>
                  <p className="mt-2 break-words text-xs leading-5 text-[var(--forma-text-muted)]">{systemArchitecture.summary}</p>
                </div>
              </div>
              <div className="mt-4">
                <SystemTreeNode node={systemArchitecture.root} depth={0} />
              </div>
            </section>
          )}

          <div className="mt-6 max-w-2xl overflow-hidden rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)]">
            <div className="grid grid-cols-3 border-b border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-4 py-2.5 text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">
              <span>Category</span>
              <span className="text-center">Parts</span>
              <span className="text-right">Cost</span>
            </div>
            <SummaryRow label="Electrical" parts={metrics.electricalParts} cost={metrics.electricalCost} />
            <SummaryRow label="Mechanical" parts={metrics.mechanicalParts} cost={metrics.mechanicalCost} />
            <SummaryRow label="Total" parts={metrics.totalParts} cost={metrics.totalCost} strong />
          </div>
        </div>
      </div>
    </div>
  );
}

function SystemTreeNode({ node, depth }: { node: Record<string, any>; depth: number }) {
  const reservedSystemIds = new Set([
    "system_id", "name", "domain", "purpose", "responsibilities", "constraints",
    "expected_component_roles", "interfaces", "connects_to", "detail_owner", "children",
  ]);
  const seenChildIds = new Set<string>();
  const children = (Array.isArray(node.children) ? node.children : [])
    .filter((child: unknown): child is Record<string, any> => Boolean(child && typeof child === "object"))
    .filter((child) => {
      const systemId = String(child.system_id || "").trim();
      const normalizedId = systemId.toLowerCase();
      if (!systemId || systemId.length > 100 || reservedSystemIds.has(normalizedId) || seenChildIds.has(normalizedId)) return false;
      seenChildIds.add(normalizedId);
      return true;
    })
    .slice(0, 32);
  const responsibilities = Array.isArray(node.responsibilities) ? node.responsibilities : [];
  const domain = String(node.domain || "system");

  return (
    <div className={depth ? "ml-3 border-l border-[var(--forma-border)] pl-3 sm:ml-5 sm:pl-5" : ""}>
      <article className="mb-3 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-page)] p-3 sm:p-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="rounded-md border border-[rgb(var(--forma-cyan-rgb)/0.3)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] px-2 py-1 text-[9px] font-medium uppercase tracking-[0.14em] text-[rgb(var(--forma-cyan-rgb))]">
            {domain}
          </span>
          <h3 className="break-words text-sm font-semibold text-[var(--forma-text-strong)]">{node.name || node.system_id}</h3>
        </div>
        <div className="mt-3 text-[9px] font-medium uppercase tracking-[0.16em] text-[var(--forma-text-muted)]">Why needed</div>
        <p className="mt-1.5 break-words text-xs leading-5 text-[var(--forma-text-secondary)]">{node.purpose}</p>
        {responsibilities.length > 0 && (
          <div className="mt-3 flex flex-wrap gap-1.5">
            {responsibilities.slice(0, 5).map((responsibility: string, index: number) => (
              <span key={`${node.system_id || node.name}-${index}`} className="max-w-full break-words rounded-md border border-[var(--forma-border)] px-2 py-1 text-[9px] font-medium text-[var(--forma-text-muted)]">
                {responsibility}
              </span>
            ))}
          </div>
        )}
      </article>
      {children.map((child: Record<string, any>, index: number) => (
        <SystemTreeNode key={`${node.system_id || "system"}-${child.system_id || "child"}-${index}`} node={child} depth={depth + 1} />
      ))}
    </div>
  );
}

export function BomPanel({
  components,
  metrics,
  cadSources = [],
  fabricationCost = 0,
  canDownloadAssets = false,
}: {
  components: any[];
  metrics: ReturnType<typeof emptyMetrics>;
  cadSources?: any[];
  fabricationCost?: number;
  canDownloadAssets?: boolean;
}) {
  return (
    <div className="h-full min-w-0 overflow-y-auto overflow-x-hidden bg-[var(--forma-page)] px-4 py-5 text-[var(--forma-text)] sm:px-5 sm:py-6">
      <div className="mx-auto flex min-w-0 max-w-[890px] flex-col gap-4">
        {components.length > 0 && (
          <div className="space-y-3 lg:hidden">
            {components.map((component) => {
              const tone = categoryTone[component.category?.toLowerCase()] || categoryTone.default;
              const Icon = iconForCategory(component.category);
              const subtotal = component.extended_price ?? (component.unit_price || 0) * (component.quantity || 1);
              const itemKey = component.line_id || component.ref_des || component.part_definition_id;

              return (
                <article key={itemKey} className="rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4">
                  <div className="flex min-w-0 items-start gap-3">
                    <span className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-md border ${tone.border} ${tone.bg}`}>
                      <Icon className={`h-5 w-5 ${tone.text}`} />
                    </span>
                    <div className="min-w-0 flex-1">
                      <h3 className="break-words text-sm font-semibold text-[var(--forma-text-strong)]">{component.name}</h3>
                      <p className="mt-1.5 break-words text-xs leading-5 text-[var(--forma-text-secondary)]">{component.rationale}</p>
                      {Array.isArray(component.instance_refs) && component.instance_refs.length > 0 && (
                        <p className="mt-1.5 break-words text-[10px] font-medium text-[var(--forma-text-muted)]">
                          {component.instance_refs.join(", ")}
                        </p>
                      )}
                      <CategoryBadge category={component.category} />
                    </div>
                  </div>

                  <div className="mt-4 grid grid-cols-2 gap-2 text-xs">
                    <div className="rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-3 py-2">
                      <div className="text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Qty</div>
                      <div className="mt-1 font-medium text-[var(--forma-text-body)]">{component.quantity}</div>
                    </div>
                    <div className="rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-3 py-2 text-right">
                      <div className="text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Subtotal</div>
                      <div className="mt-1 font-semibold text-[var(--forma-text-strong)]">~${subtotal.toFixed(2)}</div>
                    </div>
                    <div className="rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-3 py-2">
                      <div className="text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Unit</div>
                      <div className="mt-1 font-medium text-[var(--forma-text-body)]">~${Number(component.unit_price || 0).toFixed(2)}</div>
                    </div>
                    <div className="min-w-0 rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-3 py-2">
                      <div className="text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Source</div>
                      <div className="mt-2 flex flex-wrap gap-1.5">
                        {getSourcesForComponent(component).map((source) => (
                          <ComponentSourceAction
                            key={`${source.label}-${source.href}`}
                            source={source}
                            component={component}
                            canDownloadAssets={canDownloadAssets}
                            className="inline-flex justify-center px-2 py-1 text-[10px]"
                          />
                        ))}
                      </div>
                    </div>
                  </div>
                </article>
              );
            })}
          </div>
        )}

        {components.length > 0 && (
          <div className="hidden overflow-hidden rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] lg:block">
            <div className="overflow-x-auto">
              <div className="min-w-[980px]">
                <div className="grid grid-cols-[minmax(420px,1fr)_110px_110px_150px_140px] border-b border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-5 py-2.5 text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">
                  <span>Part</span>
                  <span className="text-center">Qty</span>
                  <span>Unit</span>
                  <span>Source</span>
                  <span className="text-right">Subtotal</span>
                </div>
                <div className="divide-y divide-[var(--forma-border)]">
                  {components.map((component) => (
                    <div key={component.line_id || component.ref_des || component.part_definition_id} className="grid grid-cols-[minmax(420px,1fr)_110px_110px_150px_140px] items-center px-5 py-4">
                      <div className="flex items-start gap-4">
                        <PartThumb component={component} />
                        <div className="min-w-0">
                          <h3 className="text-sm font-semibold text-[var(--forma-text-strong)]">{component.name}</h3>
                          <div className="mt-1 text-xs text-[var(--forma-text-muted)]">{component.category}</div>
                          <p className="mt-2 max-w-xl text-sm leading-6 text-[var(--forma-text-secondary)]">{component.rationale}</p>
                          {Array.isArray(component.instance_refs) && component.instance_refs.length > 0 && (
                            <p className="mt-1.5 text-xs font-medium text-[var(--forma-text-muted)]">{component.instance_refs.join(", ")}</p>
                          )}
                          <CategoryBadge category={component.category} />
                        </div>
                      </div>
                      <div className="text-center text-sm text-[var(--forma-text-body)]">{component.quantity}</div>
                      <div className="text-sm text-[var(--forma-text-body)]">~${Number(component.unit_price || 0).toFixed(2)}</div>
                      <div className="flex flex-col items-start gap-2">
                        {getSourcesForComponent(component).map((source) => (
                          <ComponentSourceAction
                            key={`${source.label}-${source.href}`}
                            source={source}
                            component={component}
                            canDownloadAssets={canDownloadAssets}
                            className="inline-flex min-w-[86px] justify-center px-3 py-1.5 text-xs"
                          />
                        ))}
                      </div>
                      <div className="text-right text-sm font-semibold text-[var(--forma-text-strong)]">~${Number(component.extended_price ?? ((component.unit_price || 0) * (component.quantity || 1))).toFixed(2)}</div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          </div>
        )}

        <div className="flex flex-col gap-1 rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] px-4 py-4 sm:flex-row sm:items-center sm:justify-between sm:px-5">
          <span className="text-[11px] font-medium uppercase tracking-[0.16em] text-[var(--forma-text-muted)]">Total Estimated Cost</span>
          <span className="text-xl font-semibold tracking-tight text-[var(--forma-text-strong)] sm:text-2xl">~${metrics.totalCost.toFixed(2)}</span>
        </div>

        <section className="rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4 sm:p-5">
          <div className="flex items-start justify-between gap-4 border-b border-[var(--forma-border)] pb-4">
            <div className="min-w-0">
              <h2 className="text-xs font-semibold uppercase tracking-[0.14em] text-[var(--forma-text-strong)]">CAD Sources</h2>
              <div className="mt-1.5 text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">3D Printed</div>
            </div>
            <div className="shrink-0 text-right">
              <div className="text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Mech Cost</div>
              <div className="mt-1 text-lg font-semibold tracking-tight text-[var(--forma-text-strong)]">~${fabricationCost.toFixed(2)}</div>
            </div>
          </div>

          <div className="mt-4 space-y-3">
            {!canDownloadAssets ? (
              <div className="rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-3 py-3 text-xs leading-6 text-[var(--forma-text-secondary)]">
                Files are available only on projects you generated.
              </div>
            ) : cadSources.length ? cadSources.slice(0, 3).map((source: any) => (
              <a
                key={`${source.name}-${source.url}`}
                href={source.url}
                target="_blank"
                rel="noreferrer"
                className="block rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] p-3 transition hover:border-[rgb(var(--forma-cyan-rgb)/0.45)]"
              >
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="break-words text-xs font-semibold text-[var(--forma-text-strong)]">{source.name}</div>
                    <div className="mt-1.5 break-words text-[11px] font-medium uppercase tracking-[0.12em] text-[rgb(var(--forma-cyan-rgb))]">{source.source_type || "CAD"} / ${(Number(source.estimated_unit_price_usd || 0)).toFixed(2)}</div>
                  </div>
                  <ExternalLink className="mt-0.5 h-4 w-4 shrink-0 text-[var(--forma-text-muted)]" />
                </div>
                {source.file_formats?.length > 0 && (
                  <div className="mt-3 flex flex-wrap gap-1.5">
                    {source.file_formats.map((format: string) => (
                      <span key={format} className="rounded-md border border-[var(--forma-border)] px-2 py-1 text-[10px] font-medium uppercase tracking-[0.12em] text-[var(--forma-text-muted)]">{format}</span>
                    ))}
                  </div>
                )}
              </a>
            )) : (
              <div className="rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-3 py-3 text-xs leading-6 text-[var(--forma-text-secondary)]">
                No CAD source records attached.
              </div>
            )}
          </div>
        </section>
      </div>
    </div>
  );
}

export function ComponentSourceAction({
  source,
  component,
  canDownloadAssets,
  className = "",
}: {
  source: { label: string; className: string; href: string; title: string };
  component: any;
  canDownloadAssets: boolean;
  className?: string;
}) {
  const isFabricationAsset = source.label.toLowerCase() === "fabricate";
  const baseClass = `${className} rounded-md font-medium transition focus:outline-none focus:ring-2 focus:ring-[rgb(var(--forma-cyan-rgb)/0.45)]`;
  if (isFabricationAsset && !canDownloadAssets) {
    return (
      <span
        title="Files are available only on projects you generated."
        className={`${baseClass} cursor-not-allowed border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] text-[var(--forma-text-muted)]`}
      >
        {source.label}
      </span>
    );
  }

  return (
    <a
      href={source.href}
      target="_blank"
      rel="noreferrer"
      aria-label={`Open ${source.label} source for ${component.name || component.part_number || "component"}`}
      title={source.title}
      className={`${baseClass} ${source.className} text-black hover:-translate-y-0.5 hover:brightness-110`}
    >
      {source.label}
    </a>
  );
}

export function MechanicalPanel({
  toggles,
  setToggles,
  electricalActive,
  setElectricalActive,
  components,
  features,
  metadata,
  mechanical,
  cadModel,
  systemArchitecture,
}: {
  toggles: Record<string, boolean>;
  setToggles: (value: any) => void;
  electricalActive: boolean;
  setElectricalActive: (value: boolean) => void;
  components: any[];
  features: string[];
  metadata: Record<string, any>;
  mechanical: Record<string, any>;
  cadModel?: Record<string, any> | null;
  systemArchitecture?: Record<string, unknown> | null;
}) {
  const visualSpec = metadata.product_visual_spec || {};
  const dimensions = mechanical.render_dimensions || visualSpec.external_dimensions_mm || metadata.render_dimensions || { x_mm: 100, y_mm: 60, z_mm: 36 };
  const placements = mechanical.component_placements || metadata.component_placements || [];
  const relationships = mechanical.spatial_relationships || metadata.spatial_relationships || [];
  const kinematics = cadModel && typeof cadModel === "object" ? cadModel.kinematics : null;
  const compliantPreview = cadModel && typeof cadModel === "object" ? cadModel.compliant_preview : null;

  return (
    <div className="relative h-full min-h-[420px] w-full overflow-hidden bg-[var(--forma-page)]">
      <MechanicalScene
        systemArchitecture={systemArchitecture}
        dimensions={dimensions}
        components={components}
        placements={placements}
        relationships={relationships}
        kinematics={kinematics}
        articulatedBodies={cadModel?.articulated_bodies}
        compliantPreview={compliantPreview}
        features={features}
        toggles={toggles}
        setToggles={setToggles}
        electricalActive={electricalActive}
        setElectricalActive={setElectricalActive}
      />
    </div>
  );
}

function issueSeverityTone(severity: unknown) {
  const key = String(severity || "").toLowerCase();
  if (key === "critical" || key === "error") {
    return {
      well: "border-[rgb(var(--forma-red-rgb)/0.35)] bg-[rgb(var(--forma-red-rgb)/0.1)]",
      chip: "border-[rgb(var(--forma-red-rgb)/0.35)] bg-[rgb(var(--forma-red-rgb)/0.1)] text-[rgb(var(--forma-red-rgb))]",
    };
  }
  if (key === "warning") {
    return {
      well: "border-[var(--forma-border)] bg-[var(--forma-surface-muted)]",
      chip: "border-[rgb(var(--forma-yellow-rgb)/0.35)] bg-[rgb(var(--forma-yellow-rgb)/0.1)] text-[rgb(var(--forma-yellow-rgb))]",
    };
  }
  if (key === "info") {
    return {
      well: "border-[var(--forma-border)] bg-[var(--forma-surface-muted)]",
      chip: "border-[rgb(var(--forma-cyan-rgb)/0.35)] bg-[rgb(var(--forma-cyan-rgb)/0.1)] text-[rgb(var(--forma-cyan-rgb))]",
    };
  }
  return {
    well: "border-[var(--forma-border)] bg-[var(--forma-surface-muted)]",
    chip: "border-[var(--forma-border)] bg-[var(--forma-surface-muted)] text-[var(--forma-text-muted)]",
  };
}

export function AssemblyPanel({
  assembly,
  issues,
}: {
  assembly: any[];
  issues: any[];
}) {
  return (
    <div className="h-full min-w-0 overflow-y-auto overflow-x-hidden bg-[var(--forma-page)] px-4 py-5 text-[var(--forma-text)] sm:px-5 sm:py-6">
      <div className="mx-auto min-w-0 max-w-[890px]">
        <div className="mb-6 border-b border-[var(--forma-border)] pb-5">
          <h2 className="break-words text-xl font-semibold tracking-tight text-[var(--forma-text-strong)]">Build Instructions</h2>
          <p className="mt-2 text-xs text-[var(--forma-text-secondary)]">Sequential assembly from the generated hardware graph.</p>
        </div>

        <div className="grid gap-4 xl:grid-cols-[1fr_280px]">
          <div className="space-y-4">
            {assembly.map((step) => (
              <section key={step.step_num} className="rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4 sm:p-5">
                <div className="flex gap-4">
                  <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] text-sm font-semibold text-[var(--forma-text-strong)]">
                    {step.step_num}
                  </span>
                  <div className="min-w-0 flex-1">
                    <h3 className="text-sm font-semibold text-[var(--forma-text-strong)]">{step.title}</h3>
                    <p className="mt-3 break-words text-sm leading-7 text-[var(--forma-text-body)]">{step.description}</p>
                    {step.danger_flag && (
                      <div className="mt-4 flex gap-2 rounded-lg border border-[rgb(var(--forma-yellow-rgb)/0.35)] bg-[rgb(var(--forma-yellow-rgb)/0.1)] p-3 text-sm leading-6 text-[rgb(var(--forma-yellow-rgb))]">
                        <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                        <span className="min-w-0 break-words">{step.danger_message || "Pay close attention to safety constraints during this stage."}</span>
                      </div>
                    )}
                    {step.affected_components?.length > 0 && (
                      <div className="mt-4 flex flex-wrap gap-2">
                        {step.affected_components.map((part: string) => (
                          <span key={part} className="rounded-md border border-[var(--forma-border)] px-2 py-1 text-[10px] font-medium text-[var(--forma-text-muted)]">
                            {part}
                          </span>
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              </section>
            ))}
          </div>

          <div className="min-w-0 rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-4 sm:p-5">
            <div className="mb-4 flex items-center gap-2">
              <ShieldCheck className="h-4 w-4 text-[rgb(var(--forma-cyan-rgb))]" />
              <h3 className="text-[11px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Safety Audit</h3>
            </div>
            {issues.length ? (
              <div className="space-y-3">
                {issues.map((issue, index) => {
                  const tone = issueSeverityTone(issue.severity);
                  return (
                    <div key={`${issue.description}-${index}`} className={`rounded-lg border p-3 ${tone.well}`}>
                      <div className="flex flex-wrap items-center gap-2">
                        <span className={`rounded-md border px-2 py-0.5 text-[10px] font-medium uppercase tracking-[0.12em] ${tone.chip}`}>
                          {issue.severity}
                        </span>
                        {issue.category && (
                          <span className="text-[10px] font-medium uppercase tracking-[0.12em] text-[var(--forma-text-muted)]">
                            {issue.category}
                          </span>
                        )}
                      </div>
                      <p className="mt-2 break-words text-xs leading-6 text-[var(--forma-text-secondary)]">{issue.description}</p>
                    </div>
                  );
                })}
              </div>
            ) : (
              <div className="rounded-lg border border-[rgb(var(--forma-green-rgb)/0.35)] bg-[rgb(var(--forma-green-rgb)/0.1)] p-4 text-xs leading-6 text-[rgb(var(--forma-green-rgb))]">
                All electrical nets validated safely.
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}


export function PartsSidebar({ components, issues, isValid }: { components: any[]; issues: any[]; isValid: boolean }) {
  return (
    <aside className="hidden min-h-0 border-l border-[#282a30] bg-[#17181d] xl:flex xl:flex-col">
      <div className="border-b border-[#282a30] p-5">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <Box className="h-4 w-4 text-slate-500" />
            <h2 className="text-sm font-black uppercase tracking-[0.2em] text-slate-400">Parts List</h2>
          </div>
          <span className="border border-[#30323a] px-2 py-1 text-[10px] text-slate-500">{components.length}</span>
        </div>
      </div>
      <div className="min-h-0 flex-1 space-y-1 overflow-y-auto px-4 py-4">
        {components.map((component, index) => {
          const tone = categoryTone[component.category?.toLowerCase()] || categoryTone.default;
          const Icon = iconForCategory(component.category);
          return (
            <div key={`${component.ref_des}-${index}`} className="flex min-w-0 items-center gap-3 py-1.5">
              <Icon className={`h-4 w-4 shrink-0 ${tone.text}`} />
              <span className="truncate text-sm font-bold text-slate-300">{component.name}</span>
            </div>
          );
        })}
      </div>
      <div className="border-t border-[#282a30] p-4">
        <div className={`flex items-center gap-2 border p-3 text-xs font-black uppercase tracking-widest ${
          isValid ? "border-emerald-500/30 bg-emerald-950/20 text-emerald-300" : "border-rose-500/30 bg-rose-950/20 text-rose-300"
        }`}>
          {isValid ? <CheckCircle className="h-4 w-4" /> : <AlertTriangle className="h-4 w-4" />}
          {isValid ? "Circuit Approved" : `${issues.length} Issues`}
        </div>
      </div>
    </aside>
  );
}

export function ImageUnavailableState({ failed = false }: { failed?: boolean }) {
  return (
    <div className="flex h-[280px] flex-col items-center justify-center bg-[var(--forma-surface-muted)] px-8 text-center sm:h-[380px]">
      <div className="flex h-16 w-16 items-center justify-center rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface)] text-[var(--forma-text-muted)]">
        {failed ? <AlertTriangle className="h-7 w-7" /> : <Box className="h-7 w-7" />}
      </div>
      <div className="mt-5 text-xs font-medium uppercase tracking-[0.16em] text-[var(--forma-text-strong)]">
        {failed ? "Product image unavailable" : "Product image not generated"}
      </div>
      <p className="mt-2 max-w-md text-xs leading-5 text-[var(--forma-text-muted)]">
        {failed
          ? "Image generation failed for this revision. The project data and build artifacts are still available."
          : "No generated product image is available for this revision."}
      </p>
    </div>
  );
}

export function SummaryRow({ label, parts, cost, strong = false }: { label: string; parts: number; cost: number; strong?: boolean }) {
  return (
    <div className={`grid grid-cols-3 border-b border-[var(--forma-border)] px-4 py-2.5 text-sm last:border-b-0 ${strong ? "font-semibold text-[var(--forma-text-strong)]" : "text-[var(--forma-text-body)]"}`}>
      <span>{label}</span>
      <span className="text-center">{parts}</span>
      <span className="text-right">${cost.toFixed(2)}</span>
    </div>
  );
}

export function CategoryBadge({ category }: { category: string }) {
  const tone = categoryTone[category?.toLowerCase()] || categoryTone.default;
  const Icon = iconForCategory(category);
  return (
    <span className={`mt-3 inline-flex items-center gap-1.5 rounded-md border ${tone.border} ${tone.bg} px-2.5 py-1 text-[10px] font-medium uppercase tracking-[0.12em] ${tone.text}`}>
      <Icon className="h-3 w-3" />
      {tone.label}
    </span>
  );
}

export function PartThumb({ component }: { component: any }) {
  const tone = categoryTone[component.category?.toLowerCase()] || categoryTone.default;
  const Icon = iconForCategory(component.category);
  return (
    <div className="flex h-16 w-16 shrink-0 items-center justify-center rounded-lg border border-[var(--forma-border)] bg-[var(--forma-surface-muted)]">
      <div className={`flex h-11 w-11 items-center justify-center rounded-md border ${tone.border} ${tone.bg}`}>
        <Icon className={`h-6 w-6 ${tone.text}`} />
      </div>
    </div>
  );
}

function getSourcesForComponent(component: any): Array<{ label: string; className: string; href: string; title: string }> {
  const category = component.category?.toLowerCase();
  const withHref = (source: { label: string; className: string }) => ({
    ...source,
    href: sourceHrefForComponent(component, source.label),
    title: sourceTitleForComponent(component, source.label),
  });

  if (category === "actuator") {
    return [
      { label: "AliExpress", className: "bg-orange-600" },
      { label: "Amazon", className: "bg-amber-400" },
      { label: "eBay", className: "bg-blue-600 text-white" },
      { label: "Newegg", className: "bg-orange-500" },
    ].map(withHref);
  }
  if (category === "power" && component.name?.toLowerCase().includes("charger")) {
    return [
      { label: "Amazon", className: "bg-amber-400" },
      { label: "eBay", className: "bg-blue-600 text-white" },
      { label: "Newegg", className: "bg-orange-500" },
    ].map(withHref);
  }
  if (category === "mechanical" || category === "3d print") {
    return [
      { label: "fabricate", className: "bg-blue-600 text-white" },
    ].map(withHref);
  }
  return [
    { label: "eBay", className: "bg-blue-600 text-white" },
    { label: "Amazon", className: "bg-amber-400" },
    { label: "Newegg", className: "bg-orange-500" },
  ].map(withHref);
}

function componentSearchText(component: any) {
  const values = [component.part_number, component.name, component.category].filter(Boolean);
  return values.join(" ").trim() || "electronic component";
}

function sourceHrefForComponent(component: any, label: string) {
  const normalizedLabel = label.toLowerCase();
  const query = encodeURIComponent(componentSearchText(component));

  if (normalizedLabel === "aliexpress") return `https://www.aliexpress.com/wholesale?SearchText=${query}`;
  if (normalizedLabel === "amazon") return `https://www.amazon.com/s?k=${query}`;
  if (normalizedLabel === "ebay") return `https://www.ebay.com/sch/i.html?_nkw=${query}`;
  if (normalizedLabel === "newegg") return `https://www.newegg.com/p/pl?d=${query}`;
  if (normalizedLabel === "fabricate") {
    const explicitUrl = firstComponentSourceUrl(component);
    return explicitUrl || `https://www.printables.com/search/models?q=${query}`;
  }

  return firstComponentSourceUrl(component) || `https://www.google.com/search?q=${query}`;
}

function sourceTitleForComponent(component: any, label: string) {
  const part = component.part_number || component.name || "component";
  if (label.toLowerCase() === "fabricate") return `Find fabrication/CAD sources for ${part}`;
  return `Search ${label} for ${part}`;
}

function firstComponentSourceUrl(component: any) {
  const candidates = [
    component.sourcing_url,
    component.source_url,
    component.supplier_url,
    component.vendor_url,
    component.purchase_url,
    component.url,
  ];
  const match = candidates.find((candidate) => typeof candidate === "string" && /^https?:\/\//i.test(candidate));
  return match || "";
}

function iconForCategory(category = "") {
  const cat = category.toLowerCase();
  if (cat === "microcontroller") return Cpu;
  if (cat === "sensor") return Database;
  if (cat === "power") return Battery;
  if (cat === "display") return Monitor;
  if (cat === "actuator") return Volume2;
  if (cat === "passives") return Sliders;
  if (cat === "mechanical") return Wrench;
  if (cat === "3d print") return Printer;
  return Box;
}

function emptyMetrics() {
  return { electricalParts: 0, mechanicalParts: 0, totalParts: 0, electricalCost: 0, mechanicalCost: 0, totalCost: 0 };
}
