from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import cadquery as cq
import matplotlib.pyplot as plt
from matplotlib.animation import PillowWriter
from mpl_toolkits.mplot3d.art3d import Poly3DCollection
import numpy as np


def load_step_triangles(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """Import a STEP with CadQuery and tessellate every contained solid."""
    model = cq.importers.importStep(str(path))
    all_vertices: list[list[float]] = []
    all_faces: list[list[int]] = []
    offset = 0
    for shape in model.vals():
        vertices, faces = shape.tessellate(0.7)
        coords = [[float(v.x), float(v.y), float(v.z)] for v in vertices]
        tris = [[int(i) + offset for i in face] for face in faces]
        all_vertices.extend(coords)
        all_faces.extend(tris)
        offset += len(coords)
    if not all_vertices or not all_faces:
        raise RuntimeError(f"No tessellated geometry found in {path}")
    return np.asarray(all_vertices, dtype=float), np.asarray(all_faces, dtype=int)


def equal_limits(vertices: np.ndarray) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    """Return equal-scale XYZ limits around the model center."""
    mins = vertices.min(axis=0)
    maxs = vertices.max(axis=0)
    center = (mins + maxs) / 2.0
    radius = max(float((maxs - mins).max()) / 2.0, 1.0) * 1.15
    return tuple((float(c - radius), float(c + radius)) for c in center)  # type: ignore[return-value]


def render(step_path: Path, gif_path: Path, png_path: Path) -> None:
    """Render a turntable GIF directly from the native STEP geometry."""
    vertices, faces = load_step_triangles(step_path)
    limits = equal_limits(vertices)
    triangles = vertices[faces]

    fig = plt.figure(figsize=(8, 8), dpi=110)
    ax = fig.add_subplot(111, projection="3d")
    ax.set_axis_off()
    ax.set_xlim(*limits[0])
    ax.set_ylim(*limits[1])
    ax.set_zlim(*limits[2])
    ax.set_box_aspect((1, 1, 1))
    ax.set_title("Current State — Forma 0.3.6 native STEP", pad=20)

    mesh = Poly3DCollection(triangles, linewidths=0.18, alpha=1.0)
    mesh.set_edgecolor((0.18, 0.18, 0.18, 0.42))
    mesh.set_facecolor((0.72, 0.75, 0.79, 1.0))
    ax.add_collection3d(mesh)

    # Baseline frame.
    ax.view_init(elev=14, azim=-72)
    fig.tight_layout()
    fig.savefig(png_path, bbox_inches="tight")

    writer = PillowWriter(fps=18)
    with writer.saving(fig, str(gif_path), dpi=110):
        for frame_index in range(72):
            azim = -72 + frame_index * 5.0
            elev = 14 + 4.0 * math.sin(frame_index / 72.0 * math.tau)
            ax.view_init(elev=elev, azim=azim)
            writer.grab_frame()

    metadata = {
        "step": str(step_path),
        "gif": str(gif_path),
        "png": str(png_path),
        "vertices": int(vertices.shape[0]),
        "triangles": int(faces.shape[0]),
        "renderer": "CadQuery STEP import -> OCCT tessellation -> matplotlib",
    }
    gif_path.with_suffix(".render.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: render_step.py STEP GIF PNG")
    render(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
