"use client";

import { OrbitControls } from "@react-three/drei";
import { Canvas } from "@react-three/fiber";
import { useEffect, useMemo, useState } from "react";
import * as THREE from "three";
import SystemHierarchy from "./system-hierarchy";
import styles from "./mechanical-scene.module.css";

import { advanceMechanismProgress, type ArticulatedBody, type ArticulatedMotion } from "../lib/articulated-motion";
import { openCadSampleAtProgress, type OpenCadMotionTrack } from "../lib/opencad-motion-preview";

function MovingBody({ body, track, progress, selected, onSelect }: {
  body: ArticulatedBody; track: OpenCadMotionTrack; progress: number; selected: boolean; onSelect: () => void;
}) {
  const geometry = useMemo(() => {
    const result = new THREE.BufferGeometry();
    result.setAttribute("position", new THREE.Float32BufferAttribute(body.vertices, 3));
    result.setIndex(body.faces);
    result.computeVertexNormals();
    result.computeBoundingBox();
    return result;
  }, [body]);
  useEffect(() => () => geometry.dispose(), [geometry]);
  const sample = openCadSampleAtProgress(track, progress);
  const markerZ = (geometry.boundingBox?.max.z ?? 0) + 0.05;
  return (
    <group name={body.targetRef} position={sample.transform.translation_mm} quaternion={sample.transform.rotation_quaternion_xyzw}>
      <mesh geometry={geometry} onClick={(event) => { event.stopPropagation(); onSelect(); }}>
        <meshStandardMaterial color={body.color} metalness={0.2} roughness={0.42} emissive={selected ? body.color : "#000000"} emissiveIntensity={0.18} />
      </mesh>
      {body.markerRadius > 0 && (
        <mesh position={[body.center[0] + body.markerRadius, body.center[1], markerZ]}>
          <circleGeometry args={[Math.max(0.6, body.markerRadius * 0.07), 20]} />
          <meshBasicMaterial color="#ffffff" />
        </mesh>
      )}
    </group>
  );
}

export default function ArticulatedMotionScene({ motion, systemArchitecture }: { motion: ArticulatedMotion; systemArchitecture?: Record<string, unknown> | null }) {
  const [progress, setProgress] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [graphicsAvailable, setGraphicsAvailable] = useState<boolean | null>(null);
  useEffect(() => {
    // Probe before mounting Canvas: renderer creation errors are asynchronous.
    try {
      const context = document.createElement("canvas").getContext("webgl2");
      setGraphicsAvailable(context !== null);
      context?.getExtension("WEBGL_lose_context")?.loseContext();
    } catch {
      setGraphicsAvailable(false);
    }
  }, []);
  const bounds = useMemo(() => {
    const box = new THREE.Box3();
    const point = new THREE.Vector3();
    for (const body of motion.bodies) {
      for (let i = 0; i < body.vertices.length; i += 3) box.expandByPoint(point.fromArray(body.vertices, i));
    }
    return { center: box.getCenter(new THREE.Vector3()), radius: box.getSize(new THREE.Vector3()).length() / 2 };
  }, [motion]);
  useEffect(() => {
    setProgress(0); setPlaying(false); setSelected(null);
  }, [motion]);
  useEffect(() => {
    if (!playing) return;
    let frame = 0;
    let last = performance.now();
    const tick = (now: number) => {
      const seconds = Math.min((now - last) / 1000, 0.1);
      last = now;
      setProgress((value) => advanceMechanismProgress(value, seconds, motion.durationSeconds, motion.loop));
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(frame);
  }, [playing, motion]);
  useEffect(() => { if (!motion.loop && progress >= 1) setPlaying(false); }, [motion.loop, progress]);

  if (graphicsAvailable !== true) {
    return (
      <div role="status" className="flex h-full min-h-[420px] items-center justify-center p-8 text-center text-sm text-[var(--forma-text-muted)]">
        {graphicsAvailable === null ? "Preparing 3D motion…" : "3D motion needs WebGL graphics support. Enable graphics acceleration in your browser or open this example in a browser with WebGL enabled."}
      </div>
    );
  }

  return (
    <div className={`${styles.layout} relative h-full w-full bg-[var(--forma-page)]`} aria-label="Articulated CAD motion preview">
      <div className={styles.panes}>
      <div className={styles.viewport}>
      <Canvas camera={{ position: [bounds.center.x + bounds.radius * 0.7, bounds.center.y - bounds.radius * 1.5, bounds.center.z + bounds.radius * 1.7], up: [0, 0, 1], fov: 42, near: 0.1, far: bounds.radius * 30 }} onPointerMissed={() => setSelected(null)}>
        <ambientLight intensity={1.4} />
        <directionalLight position={[50, -60, 150]} intensity={2.5} />
        <directionalLight position={[-80, 30, 80]} intensity={1} />
        {motion.bodies.map((body) => <MovingBody key={body.shapeId} body={body} track={motion.tracks.find((t) => t.targetRef === body.targetRef)!} progress={progress} selected={selected === body.targetRef} onSelect={() => setSelected(body.targetRef)} />)}
        <OrbitControls target={bounds.center} minDistance={bounds.radius * 0.7} maxDistance={bounds.radius * 6} enableDamping />
      </Canvas>
      </div>
      <aside className={styles.sidebar} aria-label="Components and motion controls">
      <SystemHierarchy architecture={systemArchitecture} />
      <div className="m-3 rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-3 text-xs text-[var(--forma-text-strong)]">
        <div className="mb-2 font-semibold">Assembly</div>
        {motion.bodies.map((body) => (
          <button type="button" key={body.targetRef} aria-pressed={selected === body.targetRef} onClick={() => setSelected(body.targetRef)} className="flex min-h-11 w-full items-center gap-2 rounded px-2 py-1.5 text-left hover:bg-[var(--forma-surface-muted)]">
            <span className="h-2 w-2 rounded-full" style={{ backgroundColor: body.color }} />{body.name}
          </button>
        ))}
      </div>
      <div className="m-3 rounded-xl border border-[var(--forma-border)] bg-[var(--forma-surface)] p-3 text-xs text-[var(--forma-text-strong)]">
        <div className="font-semibold">Motion Preview · {motion.label}</div>
        <div className="mt-1 text-[var(--forma-text-muted)]">{motion.bodies.length} synchronized parts · {motion.durationSeconds}s{motion.loop ? " loop" : ""}</div>
        <input type="range" min={0} max={1000} step={1} value={Math.round(progress * 1000)} aria-label="Mechanism timeline" className="my-3 w-full" onChange={(event) => { setPlaying(false); setProgress(Number(event.target.value) / 1000); }} />
        <div className="flex gap-2">
          <button type="button" aria-pressed={playing} onClick={() => { if (progress >= 1) setProgress(0); setPlaying(!playing); }} className="flex-1 rounded-md border border-[var(--forma-border)] px-3 py-2">{playing ? "Pause" : "Play"}</button>
          <button type="button" onClick={() => { setPlaying(false); setProgress(0); }} className="rounded-md border border-[var(--forma-border)] px-3 py-2">Reset</button>
        </div>
        <div className="mt-2 flex justify-between font-mono text-[10px] text-[var(--forma-text-muted)]">
          {motion.tracks.map((track) => <output key={track.id} aria-label={`${track.targetRef} angle`}>{(openCadSampleAtProgress(track, progress).value * 180 / Math.PI).toFixed(1)}°</output>)}
        </div>
      </div>
      </aside>
      </div>
    </div>
  );
}
