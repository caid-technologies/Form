# Two meshing gears

Open the **Two meshing gears** example and select **MECH**. The reference
uses a 20-tooth driver, 40-tooth driven gear, 2 mm module, 20-degree pressure
angle, 8 mm face width, 6 mm bores and 60 mm pitch-center distance.

One Play button drives both real OpenCAD meshes. The blue gear makes two turns
while the orange gear makes one in the opposite direction over six seconds.
White orientation markers make complete turns visible. Selecting either body
does not change playback. Pause, scrub and Reset apply to the whole pair.
The saved example opens even when the chat backend is unavailable. Rendering
requires WebGL 2; browsers without graphics support show an explanatory message.

In chat, request: "Create two meshing gears: 20 and 40 teeth, module 2 mm,
8 mm thick. Animate the 2:1 reduction in MECH." The authoring agent should set
`mechanical.mechanism_benchmark.kind` to `spur_gear_pair` and compile the project.
The checked-in example already contains native preview meshes; generating your
own project creates its downloadable STEP/STL/3MF/OBJ artifacts.

## Data and ownership

- OpenCAD builds the sampled involute solids and evaluates `GearCoupling`.
- The adapter samples the whole mechanism on a common timeline and exports
  individually identified meshes in `cad_model.articulated_bodies`.
- `cad_model.kinematics.mechanism` stores the coupling, cycle duration and loop
  flag; each track references the corresponding body shape ID and component ref.
- Forma displays already evaluated world poses in native Z-up coordinates.
  The R3F viewer consumes the existing pose contract directly; no viewport
  package release or client-side gear solver is required for this example.
- Both meshes and motion data survive ordinary HardwareIntermediateRepresentation JSON persistence.

Regenerate the example with:

```bash
python scripts/development/build_gear_example.py
```

## Runtime and readiness

The adapter and native CI pin OpenCAD commit
`1c417752eb42d29b951784e80f65ac79c3fb6e0e` (OpenCAD PR #118). Merge that
dependency before the Forma integration. The package reports version 0.2.4,
so setup also checks the actual gear and coupling APIs instead of trusting
the version string alone.

After deployment, update the mini-PC checkout and run the adapter's `setup`
and `check` commands using the backend's Python environment. Then generate
the example through the signed-in chat and verify MECH playback and downloads.
Record the frontend, backend and OpenCAD revisions used. A merged PR or a
passing static example does not establish that the deployed generation path
is ready.

Native tests verify valid separate solids and less than 1e-6 mm³ intersection
at seven phases through a complete tooth cycle. The profile uses discretized
involute flanks, simplified root transitions and 0.1 mm total backlash. This
is a prescribed rigid-motion reference, not a contact, load, wear or
manufacturing qualification.
