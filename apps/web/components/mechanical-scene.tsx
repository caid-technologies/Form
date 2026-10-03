"use client";

import { Html, OrbitControls } from "@react-three/drei";
import { Canvas, useThree } from "@react-three/fiber";
import { ChevronDown, Maximize2, Minimize2 } from "lucide-react";
import * as THREE from "three";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  normalizeCompliantPreviewTracks,
  normalizeOpenCadMotionTracks,
  openCadMotionRangeLabel,
  openCadSampleAtProgress,
  type OpenCadMotionTrack,
} from "../lib/opencad-motion-preview";
import { sceneAppearanceForTheme, type MechanicalSceneAppearance, type MechanicalScenePalette } from "../lib/theme";
import { useTheme } from "../lib/theme-provider";
import { normalizeArticulatedMotion } from "../lib/articulated-motion";
import ArticulatedMotionScene from "./articulated-motion-scene";
import SystemHierarchy from "./system-hierarchy";
import styles from "./mechanical-scene.module.css";

type Dimensions = { x_mm: number; y_mm: number; z_mm: number };

type ComponentInstance = {
  ref_des?: string;
  name?: string;
  category?: string;
  part_number?: string;
  rationale?: string;
};

type VectorLike =
  | Partial<Dimensions>
  | {
      x?: number;
      y?: number;
      z?: number;
      x_deg?: number;
      y_deg?: number;
      z_deg?: number;
    };

type PlacementInput = {
  ref_des?: string;
  ref?: string;
  label?: string;
  category?: string;
  layer?: string;
  position?: VectorLike;
  position_mm?: VectorLike;
  size?: VectorLike;
  size_mm?: VectorLike;
  orientation_deg?: VectorLike;
  rotation_deg?: VectorLike;
  mounting_face?: string;
  notes?: string;
};

type SpatialRelationshipInput = {
  source_ref_des?: string;
  source?: string;
  target_ref_des?: string;
  target?: string;
  relation?: string;
  axis?: string;
  offset_mm?: number;
  notes?: string;
};

type MechanicalSceneProps = {
  systemArchitecture?: Record<string, unknown> | null;
  dimensions: Dimensions;
  components: ComponentInstance[];
  placements?: PlacementInput[];
  relationships?: SpatialRelationshipInput[];
  kinematics?: unknown;
  articulatedBodies?: unknown;
  compliantPreview?: unknown;
  features: string[];
  toggles: Record<string, boolean>;
  electricalActive: boolean;
  setToggles?: (value: Record<string, boolean>) => void;
  setElectricalActive?: (value: boolean) => void;
};

type ScenePlacement = {
  refDes: string;
  label: string;
  category: string;
  layer: string;
  positionMm: [number, number, number];
  sizeMm: [number, number, number];
  rotationRad: [number, number, number];
  color: string;
  accent: string;
  component?: ComponentInstance;
  notes?: string;
};

type SceneRelationship = {
  id: string;
  sourceRef: string;
  targetRef: string;
  relation: string;
  axis: "X" | "Y" | "Z";
  offsetMm?: number;
  notes?: string;
};

type HierarchyRow = {
  placement: ScenePlacement;
  depth: number;
};

const categoryLayers: Record<string, string> = {
  microcontroller: "electrical",
  sensor: "electrical",
  actuator: "electrical",
  display: "electrical",
  power: "electrical",
  passives: "electrical",
  communication: "electrical",
  mechanical: "mechanism",
  "3d print": "print",
  default: "electrical",
};

const categoryLabels: Record<string, string> = {
  microcontroller: "MCU",
  sensor: "Sensor",
  actuator: "Actuator",
  display: "Display",
  power: "Power",
  passives: "Passives",
  communication: "Comms",
  mechanical: "Mech",
  "3d print": "3D Print",
  default: "Part",
};

const categoryOrder = [
  "enclosure",
  "microcontroller",
  "sensor",
  "actuator",
  "display",
  "power",
  "communication",
  "passives",
  "mechanical",
  "3d print",
  "default",
];

const categorySizes: Record<string, [number, number, number]> = {
  microcontroller: [38, 28, 5],
  sensor: [20, 18, 8],
  actuator: [30, 24, 14],
  display: [32, 18, 4],
  power: [42, 24, 8],
  passives: [14, 14, 7],
  communication: [28, 20, 5],
  mechanical: [14, 14, 8],
  "3d print": [26, 20, 8],
  default: [22, 18, 6],
};

function layerToggles(palette: MechanicalScenePalette): { key: string; label: string; color: string }[] {
  return [
    { key: "enclosure", label: "Enclosure", color: palette.envelope },
    { key: "print", label: "3D Print", color: palette.print },
    { key: "mechanism", label: "Mechanism", color: palette.mechanism },
    { key: "structural", label: "Structure", color: palette.structural },
    { key: "misc", label: "Misc", color: palette.misc },
    { key: "bodyRotation", label: "Auto Rotate", color: palette.rotate },
  ];
}

function categoryKey(category?: string) {
  return String(category || "default").trim().toLowerCase();
}

function categoryLabel(category?: string) {
  const key = categoryKey(category);
  return categoryLabels[key] || key.toUpperCase();
}

function isMechanicalCategory(category?: string) {
  const key = categoryKey(category);
  return key === "mechanical" || key === "3d print";
}

function isEnclosureLabel(label: string) {
  const normalized = label.toLowerCase();
  if (/screw|insert|standoff|button cap|fastener/.test(normalized)) return false;
  return /main enclosure|enclosure shell|project box|\bshell\b|\bhousing\b|\bcase\b|\bcover\b|\bbody\b/.test(normalized);
}

function isEnclosureCandidate(placement: ScenePlacement) {
  return (
    placement.layer.toLowerCase() === "enclosure" ||
    isEnclosureLabel(placement.label) ||
    /\benclosure\b|\bchassis\b|\benvelope\b/.test(placement.label.toLowerCase())
  );
}

/**
 * Enclosure keywords also match sub-assemblies ("Tilt Servo Housing"), so exactly one part
 * becomes the outer envelope — the largest that either reads as an enclosure or already spans
 * the build volume. Everything else keeps its own box.
 */
function pickEnvelopeRef(placements: ScenePlacement[], dimensions: Dimensions) {
  const spansBuildVolume = (placement: ScenePlacement) =>
    placement.sizeMm[0] >= dimensions.x_mm * 0.8 &&
    placement.sizeMm[1] >= dimensions.y_mm * 0.8 &&
    placement.sizeMm[2] >= dimensions.z_mm * 0.8;

  let envelopeRef: string | null = null;
  let bestVolume = -1;

  for (const placement of placements) {
    if (!isEnclosureCandidate(placement) && !spansBuildVolume(placement)) continue;
    const volume = boxVolume(placement.sizeMm);
    if (volume <= bestVolume) continue;
    bestVolume = volume;
    envelopeRef = placement.refDes;
  }

  return envelopeRef;
}

function getVectorValue(vector: VectorLike | undefined, axis: "x" | "y" | "z", fallback: number) {
  if (!vector) return fallback;
  const mmKey = `${axis}_mm` as keyof Dimensions;
  const degKey = `${axis}_deg` as "x_deg" | "y_deg" | "z_deg";
  const raw = (vector as Partial<Dimensions>)[mmKey] ?? (vector as { x?: number; y?: number; z?: number })[axis] ?? (vector as { x_deg?: number; y_deg?: number; z_deg?: number })[degKey];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : fallback;
}

function parseVector(vector: VectorLike | undefined, fallback: [number, number, number]): [number, number, number] {
  return [
    getVectorValue(vector, "x", fallback[0]),
    getVectorValue(vector, "y", fallback[1]),
    getVectorValue(vector, "z", fallback[2]),
  ];
}

function degreesToRadians(rotation: [number, number, number]): [number, number, number] {
  return rotation.map((value) => THREE.MathUtils.degToRad(value)) as [number, number, number];
}

function placementPalette(category: string, palette: MechanicalScenePalette) {
  const key = categoryKey(category);
  const color = palette.categories[key] || palette.categories.default;
  return { color, accent: color, layer: categoryLayers[key] || "electrical" };
}

function placementSize(component: ComponentInstance): [number, number, number] {
  const key = categoryKey(component.category);
  const name = `${component.name || ""} ${component.part_number || ""}`.toLowerCase();

  if (name.includes("battery")) return [48, 26, 8];
  if (name.includes("speaker")) return [24, 24, 10];
  if (name.includes("relay")) return [38, 26, 16];
  if (name.includes("oled") || name.includes("display")) return [34, 18, 4];
  if (name.includes("button") || name.includes("switch")) return [10, 10, 7];
  if (name.includes("screw") || name.includes("insert")) return [5, 5, 8];

  return categorySizes[key] || categorySizes.default;
}

function generatedPosition(component: ComponentInstance, index: number, components: ComponentInstance[], dimensions: Dimensions): [number, number, number] {
  const key = categoryKey(component.category);
  const electrical = components.filter((item) => !isMechanicalCategory(item.category));
  const electricalIndex = Math.max(0, electrical.findIndex((item) => item.ref_des === component.ref_des));
  const printParts = components.filter((item) => categoryKey(item.category) === "3d print");
  const mechParts = components.filter((item) => categoryKey(item.category) === "mechanical");

  const floorZ = -dimensions.z_mm * 0.28;
  const midZ = -dimensions.z_mm * 0.05;
  const topZ = dimensions.z_mm * 0.3;

  if (key === "microcontroller") return [0, 0, midZ];
  if (key === "display") return [0, -dimensions.y_mm * 0.4, topZ];
  if (key === "power") {
    const powerOffset = electrical.filter((item) => categoryKey(item.category) === "power").findIndex((item) => item.ref_des === component.ref_des);
    return [-dimensions.x_mm * 0.28 + powerOffset * Math.min(28, dimensions.x_mm * 0.18), dimensions.y_mm * 0.22, floorZ];
  }
  if (key === "sensor") {
    return [-dimensions.x_mm * 0.28 + electricalIndex * Math.min(18, dimensions.x_mm * 0.12), -dimensions.y_mm * 0.18, midZ];
  }
  if (key === "actuator") {
    return [dimensions.x_mm * 0.28, dimensions.y_mm * 0.15 - electricalIndex * Math.min(8, dimensions.y_mm * 0.12), midZ];
  }
  if (key === "passives" || key === "communication") {
    const count = Math.max(1, electrical.length - 1);
    const t = electricalIndex / count;
    return [
      -dimensions.x_mm * 0.34 + t * dimensions.x_mm * 0.68,
      -dimensions.y_mm * 0.34,
      floorZ + (electricalIndex % 2) * Math.min(10, dimensions.z_mm * 0.16),
    ];
  }
  if (key === "3d print") {
    const printIndex = Math.max(0, printParts.findIndex((item) => item.ref_des === component.ref_des));
    const isShell = isEnclosureLabel(`${component.name || ""} ${component.part_number || ""}`);
    if (isShell) return [0, 0, 0];
    return [
      -dimensions.x_mm * 0.36 + printIndex * Math.min(20, dimensions.x_mm * 0.16),
      dimensions.y_mm * 0.38,
      dimensions.z_mm * 0.22 - (printIndex % 2) * Math.min(18, dimensions.z_mm * 0.3),
    ];
  }
  if (key === "mechanical") {
    const mechIndex = Math.max(0, mechParts.findIndex((item) => item.ref_des === component.ref_des));
    const cornerX = mechIndex % 2 === 0 ? -dimensions.x_mm * 0.42 : dimensions.x_mm * 0.42;
    const cornerY = mechIndex < 2 ? -dimensions.y_mm * 0.36 : dimensions.y_mm * 0.36;
    return [cornerX, cornerY, floorZ + (mechIndex % 3) * 5];
  }

  const columns = Math.ceil(Math.sqrt(Math.max(components.length, 1)));
  const col = index % columns;
  const row = Math.floor(index / columns);
  return [
    (col / Math.max(columns - 1, 1) - 0.5) * dimensions.x_mm * 0.68,
    (row / Math.max(columns - 1, 1) - 0.5) * dimensions.y_mm * 0.6,
    midZ,
  ];
}

function normalizeProvidedPlacements(
  placements: PlacementInput[],
  components: ComponentInstance[],
  dimensions: Dimensions,
  palette: MechanicalScenePalette
) {
  const componentByRef = new Map(components.map((component) => [component.ref_des, component]));
  const parsed = placements
    .map((placement) => {
      const refDes = placement.ref_des || placement.ref;
      if (!refDes) return null;

      const component = componentByRef.get(refDes);
      const label = placement.label || component?.name || refDes;
      const category = placement.category || component?.category || "default";
      const swatch = placementPalette(category, palette);
      const sizeMm = parseVector(placement.size_mm || placement.size, component ? placementSize(component) : categorySizes.default);
      const rotationRad = degreesToRadians(parseVector(placement.orientation_deg || placement.rotation_deg, [0, 0, 0]));
      const positionMm = parseVector(placement.position_mm || placement.position, [0, 0, 0]);

      return {
        refDes,
        label,
        category,
        layer: placement.layer || swatch.layer,
        positionMm,
        sizeMm,
        rotationRad,
        color: swatch.color,
        accent: swatch.accent,
        component,
        notes: placement.notes || placement.mounting_face,
      } satisfies ScenePlacement;
    })
    .filter(Boolean) as ScenePlacement[];

  const usesPositiveOrigin =
    parsed.length > 0 &&
    parsed.every((placement) => {
      const [x, y, z] = placement.positionMm;
      return x >= 0 && y >= 0 && z >= 0 && x <= dimensions.x_mm && y <= dimensions.y_mm && z <= dimensions.z_mm;
    });

  if (!usesPositiveOrigin) return parsed;

  return parsed.map((placement) => {
    const positionMm: [number, number, number] = [
      placement.positionMm[0] - dimensions.x_mm / 2,
      placement.positionMm[1] - dimensions.y_mm / 2,
      placement.positionMm[2] - dimensions.z_mm / 2,
    ];

    return {
      ...placement,
      positionMm,
    };
  });
}

function buildScenePlacements(
  dimensions: Dimensions,
  components: ComponentInstance[],
  providedPlacements: PlacementInput[],
  palette: MechanicalScenePalette
): ScenePlacement[] {
  const normalized = normalizeProvidedPlacements(providedPlacements, components, dimensions, palette);
  const placementByRef = new Map(normalized.map((placement) => [placement.refDes, placement]));

  components.forEach((component, index) => {
    const refDes = component.ref_des || `C${index + 1}`;
    if (placementByRef.has(refDes)) return;

    const swatch = placementPalette(component.category || "default", palette);
    const label = component.name || component.part_number || refDes;
    const category = component.category || "default";
    placementByRef.set(refDes, {
      refDes,
      label,
      category,
      layer: isEnclosureLabel(label) ? "enclosure" : swatch.layer,
      positionMm: generatedPosition(component, index, components, dimensions),
      sizeMm: placementSize(component),
      rotationRad: [0, 0, 0],
      color: swatch.color,
      accent: swatch.accent,
      component,
      notes: component.rationale,
    });
  });

  return Array.from(placementByRef.values());
}

function dominantAxis(source: ScenePlacement, target: ScenePlacement): "X" | "Y" | "Z" {
  const deltas = [
    { axis: "X" as const, value: Math.abs(target.positionMm[0] - source.positionMm[0]) },
    { axis: "Y" as const, value: Math.abs(target.positionMm[1] - source.positionMm[1]) },
    { axis: "Z" as const, value: Math.abs(target.positionMm[2] - source.positionMm[2]) },
  ];
  return deltas.sort((a, b) => b.value - a.value)[0]?.axis || "X";
}

function normalizeRelationships(inputs: SpatialRelationshipInput[], placements: ScenePlacement[]) {
  const placementByRef = new Map(placements.map((placement) => [placement.refDes, placement]));
  const explicit = inputs
    .map((relationship, index) => {
      const sourceRef = relationship.source_ref_des || relationship.source;
      const targetRef = relationship.target_ref_des || relationship.target;
      if (!sourceRef || !targetRef || !placementByRef.has(sourceRef) || !placementByRef.has(targetRef)) return null;

      const axis = String(relationship.axis || dominantAxis(placementByRef.get(sourceRef)!, placementByRef.get(targetRef)!)).toUpperCase();
      return {
        id: `${sourceRef}-${targetRef}-${index}`,
        sourceRef,
        targetRef,
        relation: relationship.relation || "relative placement",
        axis: axis === "Y" || axis === "Z" ? axis : "X",
        offsetMm: relationship.offset_mm,
        notes: relationship.notes,
      } satisfies SceneRelationship;
    })
    .filter(Boolean) as SceneRelationship[];

  if (explicit.length > 0) return explicit;

  const controller = placements.find((placement) => categoryKey(placement.category) === "microcontroller") || placements.find((placement) => !isMechanicalCategory(placement.category));
  if (!controller) return [];

  return placements
    .filter((placement) => placement.refDes !== controller.refDes && !isEnclosureLabel(placement.label) && !isMechanicalCategory(placement.category))
    .slice(0, 6)
    .map((placement, index) => {
      const axis = dominantAxis(controller, placement);
      const sourcePosition = controller.positionMm[axis === "X" ? 0 : axis === "Y" ? 1 : 2];
      const targetPosition = placement.positionMm[axis === "X" ? 0 : axis === "Y" ? 1 : 2];
      return {
        id: `${controller.refDes}-${placement.refDes}-${index}`,
        sourceRef: controller.refDes,
        targetRef: placement.refDes,
        relation: "spatial offset",
        axis,
        offsetMm: Math.round(targetPosition - sourcePosition),
      } satisfies SceneRelationship;
    });
}

function hierarchyBox(placement: ScenePlacement, dimensions: Dimensions, envelopeRef: string | null) {
  if (placement.refDes === envelopeRef) {
    return {
      center: [0, 0, 0] as [number, number, number],
      size: [dimensions.x_mm, dimensions.y_mm, dimensions.z_mm] as [number, number, number],
    };
  }
  return { center: placement.positionMm, size: placement.sizeMm };
}

function boxVolume(size: [number, number, number]) {
  return Math.max(size[0], 1) * Math.max(size[1], 1) * Math.max(size[2], 1);
}

/**
 * Nests parts by geometric containment: a part's parent is the smallest other part
 * whose bounding box swallows its centre. Strict volume ordering keeps this acyclic.
 */
function buildHierarchy(placements: ScenePlacement[], dimensions: Dimensions, envelopeRef: string | null): HierarchyRow[] {
  if (!placements.length) return [];

  const boxes = new Map(placements.map((placement) => [placement.refDes, hierarchyBox(placement, dimensions, envelopeRef)]));
  const volumes = new Map(Array.from(boxes.entries()).map(([refDes, box]) => [refDes, boxVolume(box.size)]));
  const childrenByRef = new Map<string, string[]>(placements.map((placement) => [placement.refDes, []]));
  const roots: string[] = [];

  for (const child of placements) {
    const childBox = boxes.get(child.refDes)!;
    const childVolume = volumes.get(child.refDes)!;
    let parentRef = "";
    let parentVolume = Number.POSITIVE_INFINITY;

    for (const candidate of placements) {
      if (candidate.refDes === child.refDes) continue;
      const candidateVolume = volumes.get(candidate.refDes)!;
      if (candidateVolume <= childVolume || candidateVolume >= parentVolume) continue;

      const candidateBox = boxes.get(candidate.refDes)!;
      const contained = [0, 1, 2].every(
        (axis) => Math.abs(childBox.center[axis] - candidateBox.center[axis]) <= Math.max(candidateBox.size[axis], 1) / 2
      );
      if (!contained) continue;

      parentRef = candidate.refDes;
      parentVolume = candidateVolume;
    }

    if (parentRef) childrenByRef.get(parentRef)!.push(child.refDes);
    else roots.push(child.refDes);
  }

  const placementByRef = new Map(placements.map((placement) => [placement.refDes, placement]));
  const sortRefs = (refs: string[]) =>
    refs.slice().sort((a, b) => {
      const first = placementByRef.get(a)!;
      const second = placementByRef.get(b)!;
      const orderDelta = categoryOrder.indexOf(categoryKey(first.category)) - categoryOrder.indexOf(categoryKey(second.category));
      if (orderDelta !== 0) return orderDelta;
      return first.label.localeCompare(second.label);
    });

  const rows: HierarchyRow[] = [];
  const visited = new Set<string>();
  const walk = (refs: string[], depth: number) => {
    sortRefs(refs).forEach((refDes) => {
      if (visited.has(refDes)) return;
      visited.add(refDes);
      rows.push({ placement: placementByRef.get(refDes)!, depth });
      walk(childrenByRef.get(refDes) || [], depth + 1);
    });
  };

  // The envelope reads as the assembly root even when nothing geometrically contains it.
  const envelopeRoots = roots.filter((refDes) => refDes === envelopeRef);
  walk(envelopeRoots, 0);
  walk(
    roots.filter((refDes) => refDes !== envelopeRef),
    envelopeRoots.length ? 1 : 0
  );

  return rows;
}

function legendCounts(placements: ScenePlacement[], envelopeRef: string | null, palette: MechanicalScenePalette) {
  const counts = new Map<string, number>();
  placements.forEach((placement) => {
    const key = placement.refDes === envelopeRef ? "enclosure" : categoryKey(placement.category);
    counts.set(key, (counts.get(key) || 0) + 1);
  });

  return Array.from(counts.entries())
    .sort((a, b) => categoryOrder.indexOf(a[0]) - categoryOrder.indexOf(b[0]))
    .map(([key, count]) => ({
      key,
      count,
      label: key === "enclosure" ? "Enclosure" : categoryLabels[key] || key.toUpperCase(),
      color: key === "enclosure" ? palette.envelope : palette.categories[key] || palette.categories.default,
    }));
}

function worldPosition(positionMm: [number, number, number], scale: number): [number, number, number] {
  const [xMm, yMm, zMm] = positionMm;
  return [xMm / scale, zMm / scale, yMm / scale];
}

function worldSize(sizeMm: [number, number, number], scale: number): [number, number, number] {
  const [xMm, yMm, zMm] = sizeMm;
  return [
    Math.max(xMm / scale, 0.16),
    Math.max(zMm / scale, 0.08),
    Math.max(yMm / scale, 0.16),
  ];
}

function projectQuaternionToWorld(
  value: [number, number, number, number],
) {
  const projectQuaternion = new THREE.Quaternion(...value).normalize();
  const projectRotation = new THREE.Matrix4().makeRotationFromQuaternion(projectQuaternion);
  const basisSwap = new THREE.Matrix4().set(
    1, 0, 0, 0,
    0, 0, 1, 0,
    0, 1, 0, 0,
    0, 0, 0, 1,
  );
  const worldRotation = basisSwap.clone().multiply(projectRotation).multiply(basisSwap);
  return new THREE.Quaternion().setFromRotationMatrix(worldRotation).normalize();
}

function partMotionPose(
  spec: ScenePlacement,
  scale: number,
  motion: OpenCadMotionTrack | null,
  progress: number,
) {
  const localQuaternion = new THREE.Quaternion().setFromEuler(new THREE.Euler(...spec.rotationRad));

  if (!motion) {
    return {
      position: worldPosition(spec.positionMm, scale),
      quaternion: localQuaternion,
    };
  }

  const sample = openCadSampleAtProgress(motion, progress);
  const projectQuaternion = new THREE.Quaternion(
    ...sample.transform.rotation_quaternion_xyzw,
  ).normalize();
  const projectPosition = new THREE.Vector3(...spec.positionMm)
    .applyQuaternion(projectQuaternion)
    .add(new THREE.Vector3(...sample.transform.translation_mm));
  const worldQuaternion = projectQuaternionToWorld(
    sample.transform.rotation_quaternion_xyzw,
  );

  return {
    position: worldPosition(
      projectPosition.toArray() as [number, number, number],
      scale,
    ),
    quaternion: worldQuaternion.multiply(localQuaternion),
  };
}

function axisColor(axis: "X" | "Y" | "Z", palette: MechanicalScenePalette) {
  if (axis === "X") return palette.axisX;
  if (axis === "Y") return palette.axisY;
  return palette.axisZ;
}

function useDisposableGeometry<T extends THREE.BufferGeometry>(factory: () => T, deps: unknown[]) {
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const geometry = useMemo(factory, deps);
  useEffect(() => () => geometry.dispose(), [geometry]);
  return geometry;
}

function useBoxEdges(size: [number, number, number]) {
  return useDisposableGeometry(() => {
    const box = new THREE.BoxGeometry(size[0], size[1], size[2]);
    const edges = new THREE.EdgesGeometry(box);
    box.dispose();
    return edges;
  }, [size[0], size[1], size[2]]);
}

/** Pulls the camera back far enough that the whole assembly fits, whatever the project's scale. */
function ResponsiveCamera({ sceneRadius }: { sceneRadius: number }) {
  const { camera, size } = useThree();

  useEffect(() => {
    if (!(camera instanceof THREE.PerspectiveCamera)) return;

    const compact = size.width < 640;
    const fov = compact ? 50 : 38;
    const aspect = size.width / Math.max(size.height, 1);
    const verticalFov = THREE.MathUtils.degToRad(fov);
    const horizontalFov = 2 * Math.atan(Math.tan(verticalFov / 2) * aspect);
    const distance = (sceneRadius / Math.sin(Math.min(verticalFov, horizontalFov) / 2)) * (compact ? 1.15 : 1.05);

    camera.fov = fov;
    camera.position.copy(new THREE.Vector3(0.62, 0.46, 0.7).normalize().multiplyScalar(distance));
    camera.lookAt(0, 0, 0);
    camera.updateProjectionMatrix();
  }, [camera, sceneRadius, size.height, size.width]);

  return null;
}

function SceneEnvironment({ appearance, sceneRadius }: { appearance: MechanicalSceneAppearance; sceneRadius: number }) {
  return (
    <>
      <color attach="background" args={[appearance.background]} />
      <fog attach="fog" args={[appearance.fog, Math.max(sceneRadius * 4.5, 18), Math.max(sceneRadius * 13, 48)]} />
      <ambientLight intensity={appearance.ambientIntensity} />
      <hemisphereLight
        color={appearance.hemisphereSky}
        groundColor={appearance.hemisphereGround}
        intensity={appearance.hemisphereIntensity}
      />
      <directionalLight
        position={[8.5, 11, 6]}
        color={appearance.keyLight}
        intensity={appearance.keyLightIntensity}
      />
    </>
  );
}

function Envelope({
  dimensions,
  scale,
  selected,
  appearance,
}: {
  dimensions: Dimensions;
  scale: number;
  selected: boolean;
  appearance: MechanicalSceneAppearance;
}) {
  const size = useMemo<[number, number, number]>(
    () => [dimensions.x_mm / scale, dimensions.z_mm / scale, dimensions.y_mm / scale],
    [dimensions.x_mm, dimensions.y_mm, dimensions.z_mm, scale]
  );
  const edges = useBoxEdges(size);

  return (
    <group>
      <mesh>
        <boxGeometry args={size} />
        <meshBasicMaterial
          color={appearance.palette.envelope}
          transparent
          opacity={selected ? appearance.selectedFillOpacity : appearance.fillOpacity}
          depthWrite={false}
        />
      </mesh>
      <lineSegments geometry={edges}>
        <lineBasicMaterial
          color={selected ? appearance.selectedEdge : appearance.palette.envelope}
          transparent
          opacity={selected ? 0.95 : 0.6}
        />
      </lineSegments>
    </group>
  );
}

function AxisTriad({ dimensions, scale, palette }: { dimensions: Dimensions; scale: number; palette: MechanicalScenePalette }) {
  const width = dimensions.x_mm / scale;
  const height = dimensions.z_mm / scale;
  const depth = dimensions.y_mm / scale;
  const originX = -width / 2;
  const originY = -height / 2;
  const originZ = depth / 2;
  const overshoot = 1.16;
  const tick = Math.max(Math.min(width, height, depth) * 0.06, 0.08);
  const labelOffset = Math.max(Math.min(width, height, depth) * 0.08, 0.12);
  const geometry = useDisposableGeometry(() => {
    const points: number[] = [];
    const colors: number[] = [];
    const segment = (axis: "X" | "Y" | "Z", ax: number, ay: number, az: number, bx: number, by: number, bz: number) => {
      const color = new THREE.Color(axisColor(axis, palette));
      points.push(ax, ay, az, bx, by, bz);
      colors.push(color.r, color.g, color.b, color.r, color.g, color.b);
    };

    segment("X", originX, originY, originZ, originX + width * overshoot, originY, originZ);
    segment("Y", originX, originY, originZ, originX, originY, originZ - depth * overshoot);
    segment("Z", originX, originY, originZ, originX, originY + height * overshoot, originZ);

    segment("X", originX + width / 2, originY - tick, originZ, originX + width / 2, originY + tick, originZ);
    segment("Y", originX, originY - tick, originZ - depth / 2, originX, originY + tick, originZ - depth / 2);
    segment("Z", originX - tick, originY + height / 2, originZ, originX + tick, originY + height / 2, originZ);

    const buffer = new THREE.BufferGeometry();
    buffer.setAttribute("position", new THREE.Float32BufferAttribute(points, 3));
    buffer.setAttribute("color", new THREE.Float32BufferAttribute(colors, 3));
    return buffer;
  }, [depth, height, originX, originY, originZ, palette.axisX, palette.axisY, palette.axisZ, tick, width]);
  const labels: { axis: "X" | "Y" | "Z"; position: [number, number, number] }[] = [
    { axis: "X", position: [originX + width * overshoot + labelOffset, originY, originZ] },
    { axis: "Y", position: [originX, originY, originZ - depth * overshoot - labelOffset] },
    { axis: "Z", position: [originX, originY + height * overshoot + labelOffset, originZ] },
  ];

  return (
    <group>
      <lineSegments geometry={geometry}>
        <lineBasicMaterial vertexColors transparent opacity={0.72} />
      </lineSegments>
      {labels.map(({ axis, position }) => (
        <Html key={axis} center position={position} zIndexRange={[12, 0]}>
          <span
            aria-hidden="true"
            className="pointer-events-none flex h-5 w-5 items-center justify-center rounded-md border bg-[var(--forma-surface)] font-mono text-[10px] font-medium shadow-sm"
            style={{ borderColor: `${axisColor(axis, palette)}99`, color: axisColor(axis, palette) }}
          >
            {axis}
          </span>
        </Html>
      ))}
    </group>
  );
}

function PartWireframe({
  spec,
  scale,
  selected,
  faded,
  motion,
  motionProgress,
  motionActive,
  appearance,
  onSelect,
  onHover,
}: {
  spec: ScenePlacement;
  scale: number;
  selected: boolean;
  faded: boolean;
  motion: OpenCadMotionTrack | null;
  motionProgress: number;
  motionActive: boolean;
  appearance: MechanicalSceneAppearance;
  onSelect: (placement: ScenePlacement) => void;
  onHover: (placement: ScenePlacement | null) => void;
}) {
  const pose = useMemo(
    () => partMotionPose(spec, scale, motion, motionProgress),
    [motion, motionProgress, scale, spec],
  );
  const size = useMemo(() => worldSize(spec.sizeMm, scale), [spec.sizeMm, scale]);
  const edges = useBoxEdges(size);

  return (
    <group position={pose.position} quaternion={pose.quaternion}>
      <mesh
        onClick={(event) => {
          event.stopPropagation();
          onSelect(spec);
        }}
        onPointerOver={(event) => {
          event.stopPropagation();
          document.body.style.cursor = "pointer";
          onHover(spec);
        }}
        onPointerOut={() => {
          document.body.style.cursor = "";
          onHover(null);
        }}
      >
        <boxGeometry args={size} />
        <meshBasicMaterial
          color={spec.color}
          transparent
          opacity={selected ? appearance.selectedFillOpacity : motionActive ? Math.max(appearance.fillOpacity, 0.16) : appearance.fillOpacity}
          depthWrite={false}
        />
      </mesh>
      <lineSegments geometry={edges}>
        <lineBasicMaterial
          color={selected || motionActive ? appearance.selectedEdge : spec.color}
          transparent
          opacity={selected || motionActive ? 1 : faded ? 0.3 : 0.85}
        />
      </lineSegments>
    </group>
  );
}

function PartTag({
  placement,
  scale,
  motion,
  motionProgress,
}: {
  placement: ScenePlacement;
  scale: number;
  motion: OpenCadMotionTrack | null;
  motionProgress: number;
}) {
  const position = partMotionPose(placement, scale, motion, motionProgress).position;
  const size = worldSize(placement.sizeMm, scale);

  return (
    <Html center zIndexRange={[20, 0]} position={[position[0], position[1] + size[1] / 2, position[2]]}>
      <div className="pointer-events-none -translate-y-5 whitespace-nowrap rounded-md border border-[var(--forma-border)] bg-[rgb(var(--forma-chrome-rgb)/0.92)] px-2 py-1 text-[10px] font-medium text-[var(--forma-text-strong)] shadow-sm">
        <span style={{ color: placement.color }}>{placement.refDes}</span>
        <span className="mx-1.5 text-[var(--forma-text-muted)]">/</span>
        <span className="text-[var(--forma-text)]">{placement.label}</span>
      </div>
    </Html>
  );
}

function RelationshipLink({
  relationship,
  placements,
  scale,
  palette,
}: {
  relationship: SceneRelationship;
  placements: Map<string, ScenePlacement>;
  scale: number;
  palette: MechanicalScenePalette;
}) {
  const source = placements.get(relationship.sourceRef);
  const target = placements.get(relationship.targetRef);
  const geometry = useMemo(() => {
    if (!source || !target) return null;

    const start = new THREE.Vector3(...worldPosition(source.positionMm, scale));
    const end = new THREE.Vector3(...worldPosition(target.positionMm, scale));
    const midpoint = start.clone().add(end).multiplyScalar(0.5);
    const direction = end.clone().sub(start);
    const length = direction.length();
    const quaternion = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.clone().normalize());
    return { midpoint, quaternion, length };
  }, [scale, source, target]);

  if (!source || !target || !geometry || geometry.length < 0.1) return null;

  const color = axisColor(relationship.axis, palette);

  return (
    <group>
      <group position={geometry.midpoint} quaternion={geometry.quaternion}>
        <mesh>
          <cylinderGeometry args={[0.008, 0.008, geometry.length, 6]} />
          <meshBasicMaterial color={color} transparent opacity={0.55} />
        </mesh>
      </group>
      <Html center zIndexRange={[18, 0]} position={geometry.midpoint.toArray() as [number, number, number]}>
        <div className="pointer-events-none whitespace-nowrap rounded-md border border-[var(--forma-border)] bg-[rgb(var(--forma-chrome-rgb)/0.92)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--forma-text-body)] shadow-sm">
          <span style={{ color }}>{relationship.axis}</span>
          <span className="mx-1 text-[var(--forma-text-muted)]">/</span>
          {relationship.offsetMm !== undefined ? `${relationship.offsetMm}mm` : relationship.relation}
        </div>
      </Html>
    </group>
  );
}

/** Resolve the same visibility layer for both rendering and tree selection. */
function placementLayer(placement: ScenePlacement, envelopeRef: string | null): string {
  const key = categoryKey(placement.category);
  const layer = placement.layer.toLowerCase();
  if (placement.refDes === envelopeRef || layer === "enclosure" || isEnclosureLabel(placement.label)) return "enclosure";
  if (layer === "structural") return "structural";
  if (key === "3d print" || layer === "print") return "print";
  if (key === "mechanical" || layer === "mechanism") return "mechanism";
  if (layer === "misc") return "misc";
  return "electrical";
}

function visiblePlacement(
  placement: ScenePlacement,
  toggles: Record<string, boolean>,
  electricalActive: boolean,
  envelopeRef: string | null
): boolean {
  const layer = placementLayer(placement, envelopeRef);
  return layer === "electrical" ? electricalActive : Boolean(toggles[layer]);
}

function LayerChip({
  label,
  active,
  color,
  onClick,
  disabled,
}: {
  label: string;
  active: boolean;
  color: string;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      disabled={disabled}
      className={`rounded-md border px-2 py-1 text-[10px] font-medium uppercase tracking-[0.12em] transition disabled:cursor-not-allowed ${
        active ? "bg-[var(--forma-surface-muted)]" : "border-[var(--forma-border)] text-[var(--forma-text-muted)] hover:text-[var(--forma-text-strong)]"
      }`}
      style={active ? { borderColor: `${color}99`, color } : undefined}
    >
      {label}
    </button>
  );
}

export default function MechanicalScene(props: MechanicalSceneProps) {
  const motion = useMemo(() => normalizeArticulatedMotion(props.articulatedBodies, props.kinematics), [props.articulatedBodies, props.kinematics]);
  if (motion) return <ArticulatedMotionScene motion={motion} systemArchitecture={props.systemArchitecture} />;
  if (Array.isArray(props.articulatedBodies) && props.articulatedBodies.length) {
    return <div role="status" className="p-6 text-sm text-[var(--forma-text-secondary)]">Motion preview is unavailable because the saved meshes and motion tracks do not match. Regenerate the CAD model to restore the preview.</div>;
  }
  return <PlacementMechanicalScene {...props} />;
}

function PlacementMechanicalScene({
  systemArchitecture,
  dimensions,
  components,
  placements = [],
  relationships = [],
  kinematics,
  compliantPreview,
  features,
  toggles,
  electricalActive,
  setToggles,
  setElectricalActive,
}: MechanicalSceneProps) {
  const { theme } = useTheme();
  const appearance = sceneAppearanceForTheme(theme);
  const palette = appearance.palette;
  const containerRef = useRef<HTMLDivElement>(null);
  const [selectedRef, setSelectedRef] = useState<string | null>(null);
  const [hoveredRef, setHoveredRef] = useState<string | null>(null);
  const [treeOpen, setTreeOpen] = useState(true);
  const [nativeFullscreen, setNativeFullscreen] = useState(false);
  const [fallbackFullscreen, setFallbackFullscreen] = useState(false);
  const [activeMotionIndex, setActiveMotionIndex] = useState(0);
  const [motionProgress, setMotionProgress] = useState(0);
  const [motionPlaying, setMotionPlaying] = useState(false);
  const motionDirectionRef = useRef(1);
  const isFullscreen = nativeFullscreen || fallbackFullscreen;

  const scale = Math.max(Math.max(dimensions.x_mm, dimensions.y_mm, dimensions.z_mm) / 9.6, 8);
  const scenePlacements = useMemo(
    () => buildScenePlacements(dimensions, components, placements, palette),
    [components, dimensions, palette, placements]
  );
  const envelopeRef = useMemo(() => pickEnvelopeRef(scenePlacements, dimensions), [dimensions, scenePlacements]);
  const motionTracks = useMemo(() => {
    const validRefs = new Set(scenePlacements.map((placement) => placement.refDes));
    return [
      ...normalizeOpenCadMotionTracks(kinematics, validRefs),
      ...normalizeCompliantPreviewTracks(compliantPreview, validRefs),
    ];
  }, [compliantPreview, kinematics, scenePlacements]);
  const activeMotion = motionTracks[activeMotionIndex] || null;
  const visiblePlacements = useMemo(
    () => scenePlacements.filter((placement) => visiblePlacement(placement, toggles, electricalActive, envelopeRef)),
    [electricalActive, envelopeRef, scenePlacements, toggles]
  );
  const partPlacements = useMemo(() => visiblePlacements.filter((placement) => placement.refDes !== envelopeRef), [envelopeRef, visiblePlacements]);
  const visiblePlacementMap = useMemo(() => new Map(visiblePlacements.map((placement) => [placement.refDes, placement])), [visiblePlacements]);
  const sceneRelationships = useMemo(() => normalizeRelationships(relationships, visiblePlacements), [relationships, visiblePlacements]);
  const hierarchy = useMemo(() => buildHierarchy(scenePlacements, dimensions, envelopeRef), [dimensions, envelopeRef, scenePlacements]);
  const legend = useMemo(() => legendCounts(visiblePlacements, envelopeRef, palette), [envelopeRef, palette, visiblePlacements]);
  const layers = useMemo(() => layerToggles(palette), [palette]);
  const sceneRadius = useMemo(() => {
    const envelopeRadius = Math.hypot(dimensions.x_mm, dimensions.y_mm, dimensions.z_mm) / 2;
    const partRadius = partPlacements.reduce((widest, placement) => {
      const offset = Math.hypot(placement.positionMm[0], placement.positionMm[1], placement.positionMm[2]);
      const halfDiagonal = Math.hypot(placement.sizeMm[0], placement.sizeMm[1], placement.sizeMm[2]) / 2;
      return Math.max(widest, offset + halfDiagonal);
    }, 0);
    return Math.max(envelopeRadius, partRadius, 1) / scale;
  }, [dimensions.x_mm, dimensions.y_mm, dimensions.z_mm, partPlacements, scale]);

  const selectedPlacement = selectedRef ? visiblePlacementMap.get(selectedRef) || null : null;
  const hoveredPlacement = hoveredRef && hoveredRef !== selectedRef ? visiblePlacementMap.get(hoveredRef) || null : null;
  const focusedRelationships = useMemo(
    () =>
      selectedPlacement
        ? sceneRelationships.filter(
            (relationship) => relationship.sourceRef === selectedPlacement.refDes || relationship.targetRef === selectedPlacement.refDes
          )
        : [],
    [sceneRelationships, selectedPlacement]
  );
  const envelopeSelected = Boolean(selectedRef && selectedRef === envelopeRef);

  useEffect(() => {
    if (!motionTracks.length) {
      setMotionPlaying(false);
      setMotionProgress(0);
      setActiveMotionIndex(0);
      return;
    }
    if (activeMotionIndex >= motionTracks.length) {
      setActiveMotionIndex(0);
      setMotionProgress(0);
      setMotionPlaying(false);
      motionDirectionRef.current = 1;
    }
  }, [activeMotionIndex, motionTracks.length]);

  useEffect(() => {
    if (!motionPlaying || !activeMotion) return;

    let frame = 0;
    let lastStep = performance.now();
    const frameInterval = 1000 / 30;

    const tick = (now: number) => {
      if (now - lastStep >= frameInterval) {
        const deltaSeconds = Math.min((now - lastStep) / 1000, 0.1);
        lastStep = now;
        setMotionProgress((current) => {
          const next = current + motionDirectionRef.current * deltaSeconds * 0.42;
          if (next >= 1) {
            motionDirectionRef.current = -1;
            return 1;
          }
          if (next <= 0) {
            motionDirectionRef.current = 1;
            return 0;
          }
          return next;
        });
      }
      frame = window.requestAnimationFrame(tick);
    };

    frame = window.requestAnimationFrame(tick);
    return () => window.cancelAnimationFrame(frame);
  }, [activeMotion, motionPlaying]);

  useEffect(() => {
    const sync = () => setNativeFullscreen(document.fullscreenElement === containerRef.current);
    document.addEventListener("fullscreenchange", sync);
    return () => document.removeEventListener("fullscreenchange", sync);
  }, []);

  useEffect(() => {
    if (!fallbackFullscreen) return;
    const previousOverflow = document.body.style.overflow;
    const exitOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setFallbackFullscreen(false);
    };
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", exitOnEscape);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", exitOnEscape);
    };
  }, [fallbackFullscreen]);

  useEffect(() => () => {
    document.body.style.cursor = "";
  }, []);

  const toggleFullscreen = useCallback(() => {
    const node = containerRef.current;
    if (!node) return;

    if (document.fullscreenElement) {
      void document.exitFullscreen().catch(() => setNativeFullscreen(false));
      return;
    }
    if (fallbackFullscreen) {
      setFallbackFullscreen(false);
      return;
    }
    if (typeof node.requestFullscreen === "function") {
      // Safari/iOS and permission-policy blocked frames reject: fall back to an in-page overlay.
      node.requestFullscreen().catch(() => setFallbackFullscreen(true));
      return;
    }
    setFallbackFullscreen(true);
  }, [fallbackFullscreen]);

  const applyToggle = (key: string) => {
    if (!setToggles) return;
    setToggles({ ...toggles, [key]: !toggles[key] });
  };

  const dimensionLabel = `${Math.round(dimensions.x_mm)} × ${Math.round(dimensions.y_mm)} × ${Math.round(dimensions.z_mm)} mm`;

  return (
    <div
      ref={containerRef}
      data-fullscreen={isFullscreen}
      className={`${styles.layout} overflow-hidden bg-[var(--forma-page)] ${
        fallbackFullscreen ? "fixed inset-0 z-[100] h-[100dvh] w-screen" : isFullscreen ? "relative h-[100dvh] w-screen" : "relative h-full w-full"
      }`}
    >
      <div className={styles.panes}>
      <div className={styles.viewport} aria-label="3D model viewport">
      <Canvas camera={{ position: [10.5, 7.6, 11.5], fov: 38 }} dpr={[1, 2]} onPointerMissed={() => setSelectedRef(null)}>
        <SceneEnvironment appearance={appearance} sceneRadius={sceneRadius} />
        <ResponsiveCamera sceneRadius={sceneRadius} />

        <group position={[0, 0.1, 0]}>
          {toggles.enclosure && <Envelope dimensions={dimensions} scale={scale} selected={envelopeSelected} appearance={appearance} />}

          {partPlacements.map((placement) => (
            <PartWireframe
              key={placement.refDes}
              spec={placement}
              scale={scale}
              selected={placement.refDes === selectedRef}
              faded={Boolean(selectedRef) && placement.refDes !== selectedRef}
              motion={activeMotion?.targetRef === placement.refDes ? activeMotion : null}
              motionProgress={motionProgress}
              motionActive={Boolean(activeMotion?.targetRef === placement.refDes)}
              appearance={appearance}
              onSelect={(next) => setSelectedRef(next.refDes)}
              onHover={(next) => setHoveredRef(next?.refDes || null)}
            />
          ))}

          {selectedPlacement && !envelopeSelected && (
            <PartTag
              placement={selectedPlacement}
              scale={scale}
              motion={activeMotion?.targetRef === selectedPlacement.refDes ? activeMotion : null}
              motionProgress={motionProgress}
            />
          )}
          {hoveredPlacement && hoveredPlacement.refDes !== envelopeRef && (
            <PartTag
              placement={hoveredPlacement}
              scale={scale}
              motion={activeMotion?.targetRef === hoveredPlacement.refDes ? activeMotion : null}
              motionProgress={motionProgress}
            />
          )}

          {toggles.structural &&
            focusedRelationships.map((relationship) => (
              <RelationshipLink key={relationship.id} relationship={relationship} placements={visiblePlacementMap} scale={scale} palette={palette} />
            ))}

          <AxisTriad dimensions={dimensions} scale={scale} palette={palette} />
        </group>

        <OrbitControls
          enableDamping
          enablePan
          minPolarAngle={0.25}
          maxPolarAngle={1.5}
          minDistance={sceneRadius * 0.5}
          maxDistance={sceneRadius * 8}
          autoRotate={Boolean(toggles.bodyRotation)}
          autoRotateSpeed={0.26}
        />
      </Canvas>
      </div>

      <aside className={styles.sidebar} aria-label="Components and viewer controls">
        <div className="flex min-w-0 flex-col">
          <div className="flex shrink-0 items-center justify-between gap-2 border-b border-[var(--forma-border)] px-3 py-2">
            <div className="min-w-0">
              <div className="truncate text-[11px] font-semibold tracking-tight text-[var(--forma-text-strong)]">Components &amp; 3D controls</div>
              <div className="truncate text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">{dimensionLabel}</div>
            </div>
            <div className="flex shrink-0 items-center gap-1">
              <button
                type="button"
                onClick={toggleFullscreen}
                aria-pressed={isFullscreen}
                aria-label={isFullscreen ? "Exit full screen 3D view" : "View 3D model full screen"}
                title={isFullscreen ? "Exit full screen (Esc)" : "Full screen"}
                className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md border border-[var(--forma-border)] text-[var(--forma-text-muted)] transition hover:border-[var(--forma-text-strong)] hover:bg-[var(--forma-text-strong)] hover:text-[var(--forma-page)]"
              >
                {isFullscreen ? <Minimize2 className="h-3.5 w-3.5" /> : <Maximize2 className="h-3.5 w-3.5" />}
              </button>
              <button
                type="button"
                onClick={() => setTreeOpen((open) => !open)}
                aria-expanded={treeOpen}
                aria-label={treeOpen ? "Collapse assembly tree" : "Expand assembly tree"}
                title={treeOpen ? "Collapse" : "Expand"}
                className="flex h-11 w-11 shrink-0 items-center justify-center rounded-md border border-[var(--forma-border)] text-[var(--forma-text-muted)] transition hover:border-[var(--forma-text-strong)] hover:bg-[var(--forma-text-strong)] hover:text-[var(--forma-page)]"
              >
                <ChevronDown className={`h-3.5 w-3.5 transition-transform ${treeOpen ? "" : "-rotate-90"}`} />
              </button>
            </div>
          </div>

          {treeOpen && (
            <div className="flex min-h-0 flex-1 flex-col">
              <SystemHierarchy architecture={systemArchitecture} />
              <h3 className="px-3 pt-3 text-xs font-semibold text-[var(--forma-text-strong)]">All parts ({hierarchy.length})</h3>
              <p className="px-3 pt-1 text-[10px] text-[var(--forma-text-muted)]">Grouped by spatial containment. Hidden layers remain listed.</p>
              <div className="min-h-0 flex-1 overflow-y-auto px-2 py-2">
                {hierarchy.length ? (
                  hierarchy.map(({ placement, depth }) => {
                    const selected = placement.refDes === selectedRef;
                    return (
                      <button
                        key={placement.refDes}
                        type="button"
                        onClick={() => {
                          if (!visiblePlacement(placement, toggles, electricalActive, envelopeRef)) {
                            const layer = placementLayer(placement, envelopeRef);
                            if (layer === "electrical") setElectricalActive?.(true);
                            else setToggles?.({ ...toggles, [layer]: true });
                          }
                          setSelectedRef(selected ? null : placement.refDes);
                        }}
                        onPointerEnter={() => setHoveredRef(placement.refDes)}
                        onPointerLeave={() => setHoveredRef((current) => (current === placement.refDes ? null : current))}
                        aria-pressed={selected}
                        title={`${placement.refDes} / ${placement.label}`}
                        className={`flex min-h-11 w-full items-center gap-1.5 rounded-md px-1 py-2 text-left transition ${
                          selected ? "bg-[var(--forma-surface-muted)]" : "hover:bg-[var(--forma-surface-muted)]"
                        }`}
                        style={{ paddingLeft: 4 + Math.min(depth, 6) * 12 }}
                      >
                        {depth > 0 && <span className="shrink-0 font-mono text-[10px] leading-none text-[var(--forma-text-muted)]">└</span>}
                        <span
                          className="h-2.5 w-2.5 shrink-0 rounded-sm border border-[var(--forma-border)]"
                          style={{ backgroundColor: placement.refDes === envelopeRef ? palette.envelope : placement.color }}
                        />
                        <span className={`truncate text-[11px] ${selected ? "font-semibold text-[var(--forma-text-strong)]" : "text-[var(--forma-text)]"}`}>{placement.label}</span>
                      </button>
                    );
                  })
                ) : (
                  <div className="px-2 py-3 text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">No components available</div>
                )}
              </div>

              {legend.length > 0 && (
                <div className="shrink-0 border-t border-[var(--forma-border)] px-3 py-2">
                  <div className="flex flex-wrap gap-x-3 gap-y-1">
                    {legend.map((entry) => (
                      <span key={entry.key} className="flex items-center gap-1.5 text-[10px] font-medium uppercase tracking-[0.12em] text-[var(--forma-text-muted)]">
                        <span className="h-2 w-2 shrink-0 rounded-sm" style={{ backgroundColor: entry.color }} />
                        {entry.label} ({entry.count})
                      </span>
                    ))}
                  </div>
                </div>
              )}

              <div className="shrink-0 border-t border-[var(--forma-border)] px-3 py-2">
                <div className="text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Layers</div>
                <div className="mt-2 flex flex-wrap gap-1.5">
                  <LayerChip
                    label="Electrical"
                    active={electricalActive}
                    color={palette.electrical}
                    onClick={() => setElectricalActive?.(!electricalActive)}
                    disabled={!setElectricalActive}
                  />
                  {layers.map((layer) => (
                    <LayerChip
                      key={layer.key}
                      label={layer.label}
                      active={Boolean(toggles[layer.key])}
                      color={layer.color}
                      onClick={() => applyToggle(layer.key)}
                      disabled={!setToggles}
                    />
                  ))}
                </div>
              </div>

              {features.length > 0 && (
                <details className="shrink-0 border-t border-[var(--forma-border)] px-3 py-2">
                  <summary className="cursor-pointer list-none text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)] hover:text-[var(--forma-text-strong)]">
                    Design notes ({features.length})
                  </summary>
                  <ul className="mt-2 max-h-32 space-y-1.5 overflow-y-auto pr-1">
                    {features.map((feature, index) => (
                      <li key={`${index}-${feature.slice(0, 24)}`} className="text-[10px] leading-snug text-[var(--forma-text-secondary)]">
                        {feature}
                      </li>
                    ))}
                  </ul>
                </details>
              )}
            </div>
          )}
        </div>

        <div
          className="m-3 break-words rounded-md border border-[var(--forma-border)] px-2.5 py-1.5 text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]"
          title={selectedPlacement?.label}
        >
          {selectedPlacement?.label || "Tap a part for more info"}
        </div>

        {activeMotion && (
          <div className="m-3 rounded-xl border border-[var(--forma-border)] bg-[rgb(var(--forma-chrome-rgb)/0.94)] p-3 shadow-[var(--forma-card-shadow)] backdrop-blur-sm">
            <div className="flex items-start justify-between gap-3">
              <div className="min-w-0">
                <div className="text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">Motion Preview</div>
                <div className="mt-1 truncate text-xs font-semibold text-[var(--forma-text-strong)]">{activeMotion.label}</div>
              </div>
              <span className="shrink-0 rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-2 py-1 text-[9px] font-medium uppercase tracking-[0.12em] text-[var(--forma-text-muted)]">
                {activeMotion.type === "compliant" ? "compliant / approx" : activeMotion.type}
              </span>
            </div>

            {motionTracks.length > 1 && (
              <label className="mt-3 block">
                <span className="sr-only">Select mechanical motion</span>
                <select
                  value={activeMotionIndex}
                  onChange={(event) => {
                    setActiveMotionIndex(Number(event.target.value));
                    setMotionProgress(0);
                    setMotionPlaying(false);
                    motionDirectionRef.current = 1;
                  }}
                  className="w-full rounded-md border border-[var(--forma-border)] bg-[var(--forma-page)] px-2.5 py-2 text-xs text-[var(--forma-text-body)] outline-none focus:border-[var(--forma-text-muted)]"
                >
                  {motionTracks.map((motion, index) => (
                    <option key={motion.id} value={index}>
                      {motion.label}
                    </option>
                  ))}
                </select>
              </label>
            )}

            <div className="mt-3 flex items-center justify-between gap-3 text-[10px] font-medium uppercase tracking-[0.12em] text-[var(--forma-text-muted)]">
              <span>{activeMotion.targetRef} / {activeMotion.type === "compliant" ? "Forma preview" : "OpenCAD"}</span>
              <span>{openCadMotionRangeLabel(activeMotion)}</span>
            </div>

            <input
              type="range"
              min={0}
              max={100}
              step={1}
              value={Math.round(motionProgress * 100)}
              onChange={(event) => {
                setMotionPlaying(false);
                setMotionProgress(Number(event.target.value) / 100);
              }}
              aria-label={`Preview ${activeMotion.label} motion`}
              className="mt-3 w-full accent-[var(--forma-text-strong)]"
            />

            <div className="mt-3 flex items-center gap-2">
              <button
                type="button"
                onClick={() => setMotionPlaying((playing) => !playing)}
                aria-pressed={motionPlaying}
                className="inline-flex h-8 flex-1 items-center justify-center gap-2 rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface)] px-3 text-[10px] font-semibold uppercase tracking-[0.12em] text-[var(--forma-text-strong)] transition hover:border-[var(--forma-text-muted)]"
              >
                <span aria-hidden="true" className="text-xs leading-none">{motionPlaying ? "Ⅱ" : "▶"}</span>
                {motionPlaying ? "Pause" : "Play"}
              </button>
              <button
                type="button"
                onClick={() => {
                  setMotionPlaying(false);
                  setMotionProgress(0);
                  motionDirectionRef.current = 1;
                }}
                aria-label="Reset motion preview"
                title="Reset motion"
                className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface)] text-[var(--forma-text-muted)] transition hover:border-[var(--forma-text-muted)] hover:text-[var(--forma-text-strong)]"
              >
                <span aria-hidden="true" className="text-sm leading-none">↺</span>
              </button>
            </div>

            {activeMotion.notes && (
              <p className="mt-3 line-clamp-2 text-[10px] leading-snug text-[var(--forma-text-secondary)]">{activeMotion.notes}</p>
            )}
          </div>
        )}

        {selectedPlacement && (
          <div className="m-3 rounded-xl border border-[var(--forma-border)] bg-[rgb(var(--forma-chrome-rgb)/0.94)] p-3 shadow-[var(--forma-card-shadow)]">
            <div className="flex items-center gap-2 text-[10px] font-medium uppercase tracking-[0.14em]" style={{ color: selectedPlacement.color }}>
              <span>{selectedPlacement.refDes}</span>
              <span className="text-[var(--forma-text-muted)]">/</span>
              <span>{categoryLabel(selectedPlacement.category)}</span>
            </div>
            <div className="mt-1.5 truncate text-sm font-semibold tracking-tight text-[var(--forma-text-strong)]">{selectedPlacement.label}</div>
            {selectedPlacement.component?.part_number && (
              <div className="mt-1 truncate font-mono text-[10px] text-[var(--forma-text-muted)]">{selectedPlacement.component.part_number}</div>
            )}
            <div className="mt-3 grid grid-cols-3 gap-1.5 text-[10px] font-medium uppercase tracking-[0.12em]">
              {(["X", "Y", "Z"] as const).map((axis, index) => (
                <div key={axis} className="rounded-md border border-[var(--forma-border)] bg-[var(--forma-surface-muted)] px-2 py-1.5">
                  <div className="text-[var(--forma-text-muted)]">{axis}</div>
                  <div className="mt-0.5 truncate text-[var(--forma-text-body)]">{Math.round(selectedPlacement.positionMm[index])}mm</div>
                </div>
              ))}
            </div>
            {selectedPlacement.notes && <div className="mt-3 line-clamp-3 text-[10px] leading-snug text-[var(--forma-text-secondary)]">{selectedPlacement.notes}</div>}
          </div>
        )}

        <div className="m-3 text-[10px] font-medium uppercase tracking-[0.14em] text-[var(--forma-text-muted)]">
          Live 3D
        </div>
      </aside>
      </div>
    </div>
  );
}
