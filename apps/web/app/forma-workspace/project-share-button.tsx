"use client";

import { useEffect, useId, useRef, useState } from "react";
import { Share2, X } from "lucide-react";
import { sharedProjectUrl } from "../../lib/project-share";
import { revisionId } from "../../lib/project-history";
import { useProjectHistory } from "./project-history";
import styles from "./chat-project-layout.module.css";

type ShareRecord = { id: string; revision_id: string; created_at: string; revoked_at: string | null };
type SharePage = { items: ShareRecord[]; next_offset: number | null };

/** The session key prevents a response for another owner/version entering this dialog. */
export function ShareProjectButton({ projectId, title, isPrivate }: { projectId: string; title: string; isPrivate: boolean }) {
  const history = useProjectHistory();
  const id = history?.selection ? history.selection.snapshot?.revision_id : history?.config.latestRevisionId;
  if (!history || !id) return <button type="button" className={styles.button} disabled aria-label="Share project"
    title="A saved version is required to share"><Share2 className={styles.icon} /><span className={styles.buttonLabel}>Share</span></button>;
  const version = history.selection?.snapshot?.revision ?? history.config.latestRevision;
  return <ShareSession key={`${history.config.identityKey}:${projectId}:${id}`} projectId={projectId} id={id}
    title={history.selection?.snapshot?.title || title} isPrivate={isPrivate} version={version} apiUrl={history.config.apiUrl} getHeaders={history.config.getHeaders} />;
}

function ShareSession({ projectId, id, title, isPrivate, version, apiUrl, getHeaders }: {
  projectId: string; id: string; title: string; isPrivate: boolean; version: number | null;
  apiUrl: string; getHeaders: () => Promise<Record<string, string>>;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const listRequest = useRef<AbortController | null>(null);
  const mutation = useRef<AbortController | null>(null);
  const labelId = useId();
  const descriptionId = useId();
  const [links, setLinks] = useState<ShareRecord[]>([]);
  const [nextOffset, setNextOffset] = useState<number | null>(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [created, setCreated] = useState<{ id: string; url: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const endpoint = `${apiUrl}/projects/${encodeURIComponent(projectId)}/shared/${encodeURIComponent(id)}`;

  useEffect(() => () => { listRequest.current?.abort(); mutation.current?.abort(); }, []);

  async function loadLinks(offset = 0) {
    listRequest.current?.abort();
    const controller = new AbortController();
    listRequest.current = controller;
    setLoading(true);
    setError(null);
    try {
      const response = await fetch(`${endpoint}/links?offset=${offset}`, {
        headers: await getHeaders(), cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error("Could not load shared links. Try again.");
      const page: SharePage = await response.json();
      if (controller.signal.aborted) return;
      setLinks((previous) => offset ? [...new Map([...previous, ...page.items].map((link) => [link.id, link])).values()] : page.items);
      setNextOffset(page.next_offset);
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not load shared links.");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }

  async function copy(url: string, signal?: AbortSignal) {
    try {
      await navigator.clipboard.writeText(url);
      if (!signal?.aborted) setCopied(true);
    } catch {
      // The visible, selectable URL is also usable when clipboard permission is denied.
      if (!signal?.aborted) setCopied(false);
    }
  }

  async function createLink() {
    if (mutation.current) return;
    const controller = new AbortController();
    mutation.current = controller;
    setBusy(true);
    setError(null);
    setCopied(false);
    try {
      const response = await fetch(endpoint, {
        method: "POST", headers: await getHeaders(), cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error("Could not create the share link. Try again.");
      const result = await response.json();
      if (result.revision_id !== id || !revisionId(result.id) || !/^[0-9a-f]{64}$/.test(result.token)) {
        throw new Error("The share link could not be verified.");
      }
      if (controller.signal.aborted) return;
      const url = sharedProjectUrl(window.location.origin, projectId, id, result.token);
      setCreated({ id: result.id, url });
      await copy(url, controller.signal);
      if (!controller.signal.aborted) await loadLinks();
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not create the share link.");
    } finally {
      if (!controller.signal.aborted) { setBusy(false); mutation.current = null; }
    }
  }

  async function revoke(linkId: string) {
    if (mutation.current) return;
    const controller = new AbortController();
    mutation.current = controller;
    // Do not let an older list response restore an active state after revocation.
    listRequest.current?.abort();
    setLoading(false);
    setBusy(true);
    setError(null);
    try {
      const response = await fetch(`${endpoint}/links/${encodeURIComponent(linkId)}`, {
        method: "DELETE", headers: await getHeaders(), cache: "no-store", signal: controller.signal,
      });
      if (!response.ok) throw new Error("Could not revoke this link. Try again.");
      if (controller.signal.aborted) return;
      setLinks((previous) => previous.map((link) => link.id === linkId ? { ...link, revoked_at: new Date().toISOString() } : link));
      if (created?.id === linkId) { setCreated(null); setCopied(false); }
    } catch (reason) {
      if (!controller.signal.aborted) setError(reason instanceof Error ? reason.message : "Could not revoke this link.");
    } finally {
      if (!controller.signal.aborted) { setBusy(false); mutation.current = null; }
    }
  }

  return <>
    <button type="button" className={styles.button} disabled={busy} aria-label="Share project" aria-haspopup="dialog"
      onClick={() => { dialog.current?.showModal(); void loadLinks(); }}>
      <Share2 className={styles.icon} /><span className={styles.buttonLabel}>Share</span>
    </button>
    <dialog ref={dialog} className={styles.shareDialog} aria-labelledby={labelId} aria-describedby={descriptionId}>
      <header className={styles.historyHeader}>
        <h2 id={labelId}>Share {version ? `version ${version}` : "saved version"}</h2>
        <button type="button" className={styles.button} aria-label="Close sharing" onClick={() => dialog.current?.close()}><X className={styles.icon} /></button>
      </header>
      <div className={styles.shareBody}>
        <strong>{title}</strong>
        <p id={descriptionId}>Anyone with a link can view this saved version{isPrivate ? " of your private project" : ""}. Your chat stays private. Later edits won’t change it.</p>
        <button type="button" className={styles.button} disabled={busy} onClick={() => void createLink()}>Create and copy link</button>
        {created && <div className={styles.shareCreated}>
          <label>New share link<input aria-label="New share link" readOnly value={created.url} onFocus={(event) => event.currentTarget.select()} /></label>
          <button type="button" className={styles.button} onClick={() => void copy(created.url)}>Copy link</button>
          <span role="status">{copied ? "Link copied." : "Select and copy the link above."}</span>
          <small>Keep this link; you can revoke it below, but it won’t be shown again after you leave this version.</small>
        </div>}
        <h3>Links for this version</h3>
        {loading && <p role="status">Loading links…</p>}
        {!loading && !error && links.length === 0 && <p>No links have been created.</p>}
        <ul className={styles.shareLinks}>
          {links.map((link) => <li key={link.id}>
            <div><strong>Link {link.id.slice(0, 8)}</strong><time dateTime={link.created_at}>{new Date(link.created_at).toLocaleString()}</time></div>
            {link.revoked_at ? <span>Revoked</span> : <button type="button" className={styles.button} disabled={busy}
              aria-label={`Revoke link ${link.id.slice(0, 8)}`} onClick={() => void revoke(link.id)}>Revoke</button>}
          </li>)}
        </ul>
        {nextOffset !== null && <button type="button" className={styles.button} disabled={loading || busy} onClick={() => void loadLinks(nextOffset)}>Load older links</button>}
        {error && <div role="alert" className={styles.historyError}>{error}<button type="button" className={styles.button} disabled={busy} onClick={() => void loadLinks()}>Refresh links</button></div>}
      </div>
    </dialog>
  </>;
}
