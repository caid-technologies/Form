import { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import ChatProjectLayout, { ChatProjectSurface, ProjectUpdateCard } from "../../app/forma-workspace/chat-project-layout";
import { FormaAuthProvider } from "../../lib/forma-auth";
import "./chat-project-workspace.css";

declare global {
  interface Window { viewerStats: { live: number; mounts: number; unmounts: number; peak: number }; }
}
window.viewerStats = { live: 0, mounts: 0, unmounts: 0, peak: 0 };

function Viewer({ projectId }: { projectId: string }) {
  const [camera, setCamera] = useState(0);
  useEffect(() => {
    const stats = window.viewerStats;
    stats.live += 1;
    stats.mounts += 1;
    stats.peak = Math.max(stats.peak, stats.live);
    return () => { stats.live -= 1; stats.unmounts += 1; };
  }, []);
  return <div className="viewer">
    <canvas width={640} height={420} data-testid="model-canvas" aria-label={`Model for ${projectId}`} />
    <div className="model-placeholder"><span>CAD preview</span><strong>{projectId === "controller" ? "USB Game Controller" : "Mechanical Bracket"}</strong><small>Single active viewer</small></div>
    <button type="button" onClick={() => setCamera((value) => value + 1)}>Orbit camera</button>
    <output data-testid="camera-position">{camera}</output>
  </div>;
}

function Fixture() {
  const [projectId, setProjectId] = useState("controller");
  const [tab, setTab] = useState("CAD");
  const [updates, setUpdates] = useState(1);
  const [draft, setDraft] = useState("");
  const title = projectId === "controller" ? "USB Game Controller" : "Mechanical Bracket";
  return <div className="fixture-app">
    <aside className="fixture-sidebar"><strong>Forma</strong><button type="button" onClick={() => setProjectId("controller")}>Controller chat</button><button type="button" onClick={() => setProjectId("bracket")}>Bracket chat</button></aside>
    <main className="fixture-main">
      <ChatProjectLayout conversationKey={projectId} projectId={projectId} project={
        <ChatProjectSurface title={title}>
          <div className="project-tabs" role="group" aria-label="Project section">
            {["Overview", "Materials", "Mechanical", "CAD", "Electrical", "Documentation"].map((name) => <button key={name} type="button" aria-pressed={tab === name} onClick={() => setTab(name)}>{name}</button>)}
          </div>
          {tab === "CAD" ? <Viewer projectId={projectId} /> : <div className="overview">{tab} for {title}</div>}
        </ChatProjectSurface>
      }>
        <header className="chat-header"><span>Project chat</span><strong>{title}</strong></header>
        <div className="messages" data-testid="message-scroller">
          <article className="message user"><small>You</small><p>Update the game controller&apos;s CAD please.</p></article>
          {Array.from({ length: updates }, (_, index) => <article key={index} className="message assistant"><small>Forma</small><p>{index ? "The next project update is ready." : "Updated the rear shell and added trigger clearance."}</p><ProjectUpdateCard message={{ role: "assistant", status: "success", projectId }} /></article>)}
          <article className="message"><small>Earlier project</small><ProjectUpdateCard message={{ role: "assistant", status: "success", projectId: "archived/controller" }} /></article>
          <ProjectUpdateCard message={{ role: "assistant", status: "loading", projectId }} />
        </div>
        <form className="composer" onSubmit={(event) => { event.preventDefault(); setUpdates((value) => value + 1); }}>
          <textarea aria-label="Describe a project change" value={draft} onChange={(event) => setDraft(event.target.value)} placeholder="Describe a change to CAD..." />
          <button type="submit">Complete update</button>
        </form>
      </ChatProjectLayout>
    </main>
  </div>;
}

const root = document.getElementById("root");
if (!root) throw new Error("Missing fixture root");
createRoot(root).render(<FormaAuthProvider mode="local"><Fixture /></FormaAuthProvider>);
