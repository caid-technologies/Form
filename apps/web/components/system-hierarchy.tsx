"use client";

type SystemNode = {
  system_id?: string;
  name?: string;
  purpose?: string;
  children?: SystemNode[];
};

/** Show saved topology without inferring system ownership from part positions. */
export default function SystemHierarchy({ architecture }: { architecture?: Record<string, unknown> | null }) {
  const root = architecture?.root;
  if (!root || typeof root !== "object") return null;
  return (
    <section aria-label="System hierarchy" className="min-w-0 border-b border-[var(--forma-border)] p-3">
      <h3 className="mb-2 text-xs font-semibold text-[var(--forma-text-strong)]">System hierarchy</h3>
      <SystemBranch node={root as SystemNode} />
    </section>
  );
}

/** Native disclosures keep every branch independently keyboard accessible. */
function SystemBranch({ node }: { node: SystemNode }) {
  const children = Array.isArray(node.children) ? node.children.filter((child) => child && typeof child === "object") : [];
  return (
    <details open className="min-w-0 text-xs text-[var(--forma-text)]">
      <summary className="min-h-11 cursor-pointer break-words py-3 font-medium">{node.name || node.system_id || "System"}</summary>
      {node.purpose && <p className="mb-2 break-words text-[11px] text-[var(--forma-text-muted)]">{node.purpose}</p>}
      {children.length > 0 && (
        <div className="ml-2 border-l border-[var(--forma-border)] pl-2">
          {children.map((child, index) => <SystemBranch key={`${child.system_id || "system"}-${index}`} node={child} />)}
        </div>
      )}
    </details>
  );
}
