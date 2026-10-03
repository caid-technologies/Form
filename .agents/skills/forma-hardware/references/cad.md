# Forma CAD

The Forma hardware skill manages OpenCAD for CAD-capable workflows. The base
`caid-forma-core` package does not install OpenCAD.

## Supported runtime

- OpenCAD: `0.2.4`
- Required extra: `occt`
- Managed source: OpenCAD gear motion commit `1c417752eb42d29b951784e80f65ac79c3fb6e0e`
- Python: `3.11+` for the Forma skill environment

The OCCT extra is required for real STEP and STL exchange files. The analytic
OpenCAD backend is not a substitute for an exported CAD artifact.

## Setup and verification

Run setup once when installing the skill, before starting a CAD generation
workflow:

```bash
python <skill-directory>/scripts/cad.py setup
```

Setup first reuses an installed OpenCAD `0.2.4` runtime only when native OCCT, rigid kinematics, spur gears and gear coupling are available. Otherwise it installs the pinned OpenCAD gear-motion commit and
verifies the backend before returning. To verify without changing the active
Python environment:

```bash
python <skill-directory>/scripts/cad.py check
```

If setup fails, the command reports one exact recovery command. Keep the
command's Python interpreter and environment consistent with the interpreter
used to invoke `cad.py`.

## Build an artifact

Have the agent write a readable OpenCAD model and use the adapter for export:

```bash
python <skill-directory>/scripts/cad.py build PROJECT/assembly.py PROJECT/outputs/assembly.step --tree-output PROJECT/outputs/assembly.tree.json
python <skill-directory>/scripts/cad.py build PROJECT/assembly.py PROJECT/outputs/assembly.stl
```

The adapter creates an OCCT runtime, runs the model, validates the exchange
file structure, and publishes the output atomically. Existing outputs are not
replaced unless `--force` is supplied. Keep the model source and feature tree
with the artifact so the result can be rebuilt.

## OpenCAD source overrides

An existing compatible installation is always preferred. For a private wheel,
mirror, or local package that still reports the supported `0.2.4` version, set
`FORMA_OPENCAD_REQUIREMENT` for setup and build:

```bash
FORMA_OPENCAD_REQUIREMENT='opencad[occt] @ file:///path/to/opencad-0.2.4-py3-none-any.whl' python <skill-directory>/scripts/cad.py setup
```

The override changes only the package source/installer requirement. The adapter still rejects any runtime whose reported OpenCAD version is not `0.2.4`, whose OCCT backend cannot be constructed, or which does not expose the rigid kinematics module.
