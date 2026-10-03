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
| CAD export | Yes | STEP/STL/3MF/OBJ paths supported | No fluid-network semantics or runtime bindings |
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

# Liquid / fluid systems: confirmed primary capability gap

## What exists

CAID already contains examples and data with:

- water pumps
- electrically controlled solenoid valves
- reservoirs
- irrigation / hydroponic plumbing concepts
- heaters and temperature sensors
- plumbing-related assembly instructions

This proves the design stack can **name and source fluid-handling parts** and can describe some of their electrical control.

## What does not exist

There is currently no first-class liquid/fluid-system representation covering:

- fluid source / reservoir
- fluid identity and compatibility
- tank capacity and current volume
- pump
- pump flow rate / head / operating region
- valve and valve state
- tubing / hose / pipe
- fittings and junctions
- manifold
- flow direction
- nominal and measured flow rate
- pressure
- restriction / pressure drop
- priming state
- liquid temperature
- heater-to-fluid thermal coupling
- level sensing
- flow sensing
- leak detection
- sink / dispensing endpoint
- drain / waste path
- rinse / cleaning path
- contamination / cross-contact boundaries
- empty-reservoir behavior
- blocked-line behavior
- pump-dry behavior
- transfer completion criteria

There is also no fluid schematic/netlist equivalent to the electrical netlist.

**This is the primary baseline gap:** CAID can currently put pumps, valves, reservoirs, and heaters into a design, but it cannot yet represent the liquid circuit as a typed system, calculate or simulate its behavior, or bind that system consistently into firmware, telemetry, and a physical backend.

---

# Why the matcha machine is a useful benchmark

A normal robot-arm benchmark can accidentally stay inside capabilities CAID already has:

\`\`\`text
motors + CAD + joints + wiring + animation
\`\`\`

The matcha machine forces multiple physical domains to interact:

\`\`\`text
electrical
+ mechanical
+ thermal
+ liquid storage and transfer
+ flow control
+ sensing
+ control
+ mixing
+ serving
+ rinse / waste handling
+ sanitation / service constraints
\`\`\`

For that reason, the benchmark should **explicitly require a liquid-handling subsystem**. A design that merely places a pump in the BOM without producing a coherent fluid path must not count as closing the gap.

## Required liquid benchmark subsystem

The reference matcha machine must model the complete operational liquid path, at minimum:

\`\`\`text
water source / reservoir
        ↓
level sensing
        ↓
pump
        ↓
heater / heated volume
        ↓
temperature sensing
        ↓
valve / controlled dispensing path
        ↓
mixing vessel
        ↓
transfer / dispense path
        ↓
cup
        ↓
rinse / drain / waste path
\`\`\`

If milk is included, it must be represented as a second fluid path with explicit separation from water until the intended mixing point.

The generated project must include, at minimum:

- reservoir capacity
- pump
- tubing / fluid path
- valve or equivalent controlled flow element
- heater-to-liquid relationship
- temperature sensing
- volume / level accounting
- dispense target volumes
- drain / waste handling
- rinse / cleaning path
- empty-reservoir behavior
- blocked-flow handling
- leak / unexpected-loss fault handling
- safe heater behavior when liquid is absent

The simulator must model at minimum:

- reservoir depletion
- fluid transfers between containers/nodes
- pump flow
- valve state
- water temperature
- heating and cooling
- target dispense volume
- line blocked / no-flow fault
- empty reservoir
- leak / unexpected volume loss
- pump timeout
- heater-without-water rejection
- rinse / drain progression

The runtime must expose these values through the same telemetry contract used by the eventual physical implementation.

---

# Liquid-system desired state

A future runtime representation should be able to express something equivalent to:

\`\`\`json
{
  "system_id": "fluid.matcha",
  "fluids": [
    {"id": "water", "kind": "water"}
  ],
  "nodes": [
    {
      "id": "water_reservoir",
      "type": "reservoir",
      "fluid_id": "water",
      "capacity_ml": 2000
    },
    {
      "id": "heater_volume",
      "type": "heated_vessel",
      "fluid_id": "water"
    },
    {
      "id": "mixing_vessel",
      "type": "vessel"
    },
    {
      "id": "cup",
      "type": "sink"
    },
    {
      "id": "waste",
      "type": "waste"
    }
  ],
  "edges": [
    {
      "id": "water_feed",
      "source": "water_reservoir",
      "target": "heater_volume",
      "pump_id": "water_pump"
    },
    {
      "id": "hot_water_dispense",
      "source": "heater_volume",
      "target": "mixing_vessel",
      "valve_id": "water_valve"
    },
    {
      "id": "drink_dispense",
      "source": "mixing_vessel",
      "target": "cup"
    },
    {
      "id": "rinse_drain",
      "source": "mixing_vessel",
      "target": "waste"
    }
  ]
}
\`\`\`

The runtime should be able to derive or validate:

- available liquid volume
- requested transfer volume
- flow direction
- expected transfer time
- no-flow / blocked-flow condition
- impossible volume creation
- leak / volume imbalance
- heater safety based on liquid presence
- target temperature and temperature error
- rinse completion
- incompatible-fluid routing
- pump/valve timeout behavior

---

# Pneumatics: useful secondary capability gap

Pneumatics is still valuable, but it is **not the primary requirement of this matcha benchmark**.

A CAID-wide search shows only incidental/raw references such as pneumatic tires and one source plan mentioning a pneumatic launcher. There is no typed pneumatic circuit model for compressors, regulators, valves, cylinders, pressure zones, flow, or safe depressurization.

That makes pneumatics a useful **stretch benchmark** or optional implementation choice for mechanisms such as:

- cup presentation
- ingredient gate actuation
- clamping
- cleaning/blow-off
- pick-and-place

If a matcha-machine implementation chooses pneumatic actuation, CAID should preserve and model it rather than flattening it into a generic actuator. But the benchmark **must not require pneumatics** and must not reject an otherwise valid electric/mechanical serving actuator.

---

# Snapshot conclusion

As of this snapshot, CAID has enough infrastructure to generate and visualize sophisticated electromechanical designs, and enough isolated research work to prove physics, correction, validation, and agent supervision independently.

The missing step is a **multi-domain executable machine model**.

The clearest concrete gap exposed by the matcha benchmark is liquid handling:

> We can name pumps, valves, reservoirs, heaters, and tubing-related parts, but we cannot yet represent the liquid network as a typed circuit, calculate its transfers, bind it to control logic, simulate reservoir/flow/thermal state, stream that state, validate fluid-specific faults, or drive the same contract against a physical machine.

Pneumatics remains a valuable secondary domain to add after or alongside this work, but **liquid/fluid systems are the required capability this benchmark is intended to force into the stack**.
