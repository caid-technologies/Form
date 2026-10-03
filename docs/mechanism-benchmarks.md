# Additive Mechanism Benchmarks

Forma includes two bounded mechanical benchmarks for issue #525. They are not universal printer recipes; they are calibration-oriented reference geometries intended to exercise CAD generation, manufacturing exports, and Motion Preview.

## Print-in-place captive hinge

A **print-in-place mechanism** contains separate moving bodies that are manufactured already assembled. The benchmark uses two OpenCAD bodies:

- a fixed leaf with two outer hollow knuckles,
- a moving leaf with a center knuckle, captive pin, and retaining caps.

The default benchmark exposes these parameters in `mechanical.mechanism_benchmark`:

- `hinge_length_mm`
- `barrel_diameter_mm`
- `pin_diameter_mm`
- `radial_clearance_mm`
- `axial_clearance_mm`
- `wall_thickness_mm`
- `leaf_width_mm`
- `leaf_thickness_mm`
- `travel_deg`
- `stop_thickness_mm`

The two bodies remain distinct in STEP and are exported together in STEP/STL/3MF/OBJ. OpenCAD owns the revolute joint and evaluates the motion samples consumed by Forma's Motion Preview.

### Printing assumptions

Clearance depends on printer calibration, nozzle/voxel size, material, layer height, orientation, cooling, elephant-foot compensation, and support/stringing behavior. The default radial and axial values are example starting points only.

After printing, let the part cool fully. Remove obvious stringing or support debris and free the hinge gradually. If the joint is fused, do not force it; increase/calibrate clearance and reprint. The printable stop lands are contact features, but the exact stop angle is not yet collision/interference validated.

## Monolithic flexure hinge

A **monolithic compliant mechanism** is one continuous solid that moves through elastic deformation. The benchmark has two thick rigid regions joined by one thin flexure web.

Parameters include:

- `flexure_thickness_mm`
- `flexure_width_mm`
- `flexure_length_mm`
- `rigid_body_length_mm`
- `rigid_body_width_mm`
- `rigid_body_thickness_mm`
- `bend_direction`
- `nominal_travel_deg`

The exported CAD is one connected solid. Forma exposes an approximate `compliant_preview` that moves the rigid-tip proxy around the flexure center. This is intentionally separate from OpenCAD rigid kinematics.

The compliant preview is **not structural validation**. It does not compute the physically deformed solid, strain, stress, fatigue life, creep, yielding, layer adhesion, or anisotropy. Material and print-process validation are required before treating any travel angle as safe.

## Terminology

- **Print-in-place mechanism** — multiple moving bodies printed already assembled with intentional clearance.
- **Compliant mechanism** — motion produced by elastic deformation.
- **Monolithic compliant mechanism** — compliant mechanism made as one continuous body.
- **Living hinge / flexure hinge** — a thin region designed to flex repeatedly.

## Example projects

- `examples/print_in_place_hinge.json`
- `examples/monolithic_flexure_hinge.json`

Web deep links:

- `/?example=print_in_place_hinge&tab=mechanical`
- `/?example=monolithic_flexure_hinge&tab=mechanical`

## What this establishes

These benchmarks are the base for later printer-aware clearance recommendations, interference checks, range-of-motion validation, and compliant stress/strain analysis.
