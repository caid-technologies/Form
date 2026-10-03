# CAID Current Built State Snapshot — Matcha Machine Benchmark

**Snapshot date:** 2026-10-02 ET  
**Purpose:** Freeze the current CAID capability surface before implementing the automated matcha-machine epic, so later PRs can show exactly what changed rather than moving the baseline.

## Snapshot pins

| Repository | Snapshot commit |
| --- | --- |
| Form-OSS | `0b39f010` |
| OpenCAD | `511df4ff` |
| Sim-Correct | `d2870e93` |
| Open-Industries | `3cfe521c` |
| forma-datasets | `bc9fadc8` |
| local-server-config | `6ddf3dcc` |
| Printability | `c0224ef2` |
| OpenCAD-Examples | `10f61706` |
| Parti-Agent | `195df485` |
| Parti-Vision | `ba3653b3` |
| Parti-Base | `fb5c257b` |

This snapshot is about **implemented abstractions**, not whether a word or component happens to appear in a dataset.

---

## Current end-to-end capability

Today CAID is strongest at:

```text
prompt / image / document
        ↓
Form structured hardware design
        ↓
components + BOM + wiring + hierarchy + assembly + validation
        ↓
OpenCAD geometry / STEP / STL / assembly tree
        ↓
render / turntable / authored motion / collision-checked scene playback
        ↓
optional spatial review in Open-Industries
```

Additional capabilities exist beside that path:

- **Sim-Correct**: MuJoCo-based physics fault detection, parameter identification, correction, and verification for specific robot scenarios.
- **Printability**: deterministic manufacturability checks and fix packs.
- **CAID host runtime**: command leasing, capabilities, heartbeats, supervision, and project policy for OpenCode authoring.
- **Parti / datasets**: structured hardware/product examples, validation gates, multimodal generation/evaluation, and generalized IR experiments.

What does **not** yet exist is a general machine-runtime layer that binds those outputs into one executable machine.

---

## Built-state matrix

| Domain | Built now | Level | Missing for machine generation |
| --- | --- | --- | --- |
| Product/system hierarchy | Yes | Typed | Runtime systems are not first-class |
| Component identity | Yes | Typed physical instances | Runtime sensor/actuator binding contract |
| BOM / sourcing | Yes | Typed/generated | Runtime replacement/service semantics |
| Electrical wiring/netlist | Yes | Typed + validated | General power electronics/runtime driver generation still incomplete |
| Mechanical geometry | Yes | Native CAD + assembly | Dynamics/loads are not generally solved |
| Assembly instructions | Yes | Generated | Commissioning/runtime calibration sequence |
| CAD export | Yes | STEP/STL/3MF/OBJ paths supported | None specific to pneumatics |
| Kinematic scene interaction | Yes | MOVE/GRASP/PLACE/RELEASE | Not a machine command/control model |
| Collision checking | Yes | Scene playback checks | Not full dynamics/contact/safety |
| Render/video | Yes | CAD turntables + scene animation | Live state-driven digital twin |
| Firmware namespace/planning | Partial | Architecture/context domain exists | Generated executable controller/HAL is not a canonical output |
| Agent workflow events | Yes | Pipeline events | Not physical-machine telemetry |
| Runtime telemetry | No | — | Sensor/actuator/state stream contract |
| Runtime state machine | No | — | Typed states, guards, transitions, timeouts, faults |
| Machine action API | No | — | Semantic action definitions + receipts |
| Physical backend | No | — | MCU/PLC/bridge implementing same API as simulator |
| Deterministic machine simulator | No | — | Process models + runtime backend |
| General physics backend | Partial | Sim-Correct/MuJoCo scenarios | Generic adapter from generated machine |
| Fault injection | Partial | Sim-Correct scenarios | Generic typed runtime faults |
| Sim-to-real parameter correction | Yes, narrow | CAID design artifact + patches | Generic runtime/sensor binding |
| Manufacturing validation | Partial/strong for selected processes | Rule/model based | Appliance-, wet-, pressure-, food-contact-specific validation |
| OS/process sandbox for operator | Not in CAID authoring policy | Capability/permission layer only | OpenShell or equivalent runtime isolation |

---

# Actuation / physical-domain snapshot

This benchmark exists specifically because our current abstraction set is biased toward **electrical + mechanical geometry**.

## Electric actuation

**Current state: partial but real.**

CAID datasets and examples contain:

- DC motors
- servos
- steppers
- relay-switched loads
- electric solenoids
- pumps
- heaters
- fans
- endstops and other sensors

Form can represent these as components and electrical connections. OpenCAD can model their mechanical geometry. What is missing is a generic runtime actuator model that describes command semantics, limits, feedback, and state.

## Thermal systems

**Current state: representable, not a full runtime domain.**

Heaters and temperature sensors exist in data/examples, and Form can express safety notes and electrical control relationships. There is no reusable generated thermal plant model such as:

```text
heater power
thermal mass
fluid mass
ambient temperature
heat loss
temperature sensor
control loop
hard over-temperature cutoff
```

The matcha simulator therefore needs to introduce a deterministic thermal model.

## Liquid / fluid systems

**Current state: components exist; the system abstraction does not.**

CAID data already contains examples such as hydroponic systems with:

- water pumps
- solenoid valves
- reservoirs
- plumbing-related assembly instructions

That proves the generator/datasets know these parts exist.

But we **do not have a typed fluid-network representation**. There is no canonical concept of:

```text
source/reservoir
fluid line
pump
valve
junction/manifold
flow direction
flow rate
pressure
restriction
sink
leak/fault
fluid compatibility
```

A pump appearing in a BOM is not the same as CAID understanding a fluid system.

---

# Pneumatics: confirmed capability gap

## What exists

A CAID-wide search finds:

- raw dataset references to **pneumatic tires**
- one source dataset plan that mentions a **pneumatic launch system**
- many **electrical solenoid valves** used for liquid control

These are data/examples, not a pneumatic modeling capability.

## What does not exist

There is currently no first-class pneumatic subsystem in Form/OpenCAD/runtime schemas for:

- compressor or external compressed-air source
- receiver / accumulator
- regulator
- filter / dryer
- pressure relief
- pressure sensor / switch
- manifold
- directional-control valve
- proportional valve
- tubing / hose
- fittings
- pneumatic cylinder
- rotary pneumatic actuator
- vacuum ejector / vacuum cup
- exhaust / muffler
- pressure zones
- flow paths
- pressure drop
- bore / stroke
- extension/retraction state
- end-of-stroke sensing
- force from pressure × piston area
- flow-dependent actuation time
- leak/failure behavior
- safe depressurization

There is also no pneumatic schematic/netlist equivalent to the electrical netlist.

**This is the important baseline:** CAID can currently put a pneumatic component name in a BOM or free-text plan, but it cannot yet generate, validate, simulate, or operate a pneumatic system as a system.

---

# Why the matcha machine is a useful benchmark

A normal robot-arm benchmark can accidentally stay inside capabilities CAID already has:

```text
motors + CAD + joints + wiring + animation
```

The matcha machine forces multiple physical domains to interact:

```text
electrical
+ mechanical
+ thermal
+ liquid flow
+ sensing
+ control
+ serving motion
+ sanitation/service constraints
+ pneumatic actuation
```

For that reason, the benchmark should **explicitly require a pneumatic subsystem** rather than merely allowing the model to choose one. Otherwise the generated design can avoid the missing capability and the benchmark will fail to measure the gap.

## Required pneumatic benchmark subsystem

The reference matcha machine should include at least one pneumatic actuator in the operational path, for example a pneumatic serving slide / cup-presenting mechanism.

The generated project must therefore design:

```text
compressed-air source
        ↓
receiver / regulator / relief
        ↓
pressure sensing
        ↓
valve / manifold
        ↓
tubing + fittings
        ↓
double-acting or spring-return cylinder
        ↓
serving mechanism
        ↓
position/end-stop feedback
```

The exact implementation may evolve, but the benchmark must not pass by silently replacing the pneumatic subsystem with a servo.

---

# Pneumatic desired state

A future runtime representation should be able to express something equivalent to:

```json
{
  "system_id": "pneumatic.serving",
  "supply": {
    "source": "compressor_1",
    "regulated_pressure_kpa": 400
  },
  "actuators": [
    {
      "id": "serve_cylinder",
      "type": "double_acting_cylinder",
      "bore_mm": 20,
      "stroke_mm": 120,
      "max_pressure_kpa": 700
    }
  ],
  "valves": [
    {
      "id": "serve_valve",
      "type": "5_2_solenoid",
      "ports": ["P", "A", "B", "EA", "EB"]
    }
  ],
  "sensors": [
    {"id": "supply_pressure", "type": "pressure"},
    {"id": "serve_extended", "type": "position_switch"},
    {"id": "serve_retracted", "type": "position_switch"}
  ]
}
```

And the runtime should derive or validate:

- required force
- pressure range
- cylinder bore/stroke
- valve state
- estimated air consumption
- estimated extension/retraction time
- safe maximum pressure
- timeout behavior
- pressure-loss fault
- stuck-valve fault
- cylinder jam
- impossible sensor combinations
- safe depressurization state

---

# Snapshot conclusion

As of this snapshot, CAID has enough infrastructure to generate and visualize a sophisticated electromechanical design, and enough isolated research work to prove physics, correction, validation, and agent supervision independently.

The missing step is a **multi-domain executable machine model**.

Pneumatics is the clearest concrete example of the current drift:

> We can name pneumatic hardware, but we cannot yet represent the pneumatic circuit, calculate its behavior, bind it to control logic, simulate it, stream its state, validate its safety constraints, or drive the same contract against a physical machine.

The matcha-machine epic should close that gap rather than merely produce another impressive CAD assembly.
