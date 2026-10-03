# Validation

Validation is a rule-based safety layer that checks the generated netlist before it is finalized. It protects beginners from unsafe wiring and provides structured feedback for repair loops.

## Electrical rules (current MVP)
Validation logic lives in `forma_core/validation.py` (re-exported by `apps/api/validation.py`) and includes:
- **Missing Electrical Data:** Electrical components without declared pins, or an electrical BOM without nets, produce CRITICAL findings. Module external terminals must be modelled; internal module circuitry is not required. Power-rail descriptions do not replace nets.
- **Short Circuit Detection:** Power directly connected to ground.
- **Voltage Mismatch:** Mixed logic voltages on the same net.
- **Unpowered ICs:** Active components without power or ground.
- **Pin Conflict:** A single signal pin reused across multiple nets.
- **Overcurrent Risk:** High-draw actuators powered from MCU rails.

## Severity levels
- **CRITICAL:** Must be fixed before the project is considered valid.
- **WARNING:** Risky but potentially acceptable with user intent.
- **INFO:** Non-blocking recommendations.

Empty project drafts remain saveable, and pinless mechanical BOM items do not require electrical nets. These completeness checks are not a physical verification or a claim that an empty draft is build-ready. OpenCode update and compile tools persist incomplete designs with `is_valid: false` and blocking findings so the author can repair them; caller-supplied validation claims are recomputed.

## Repair loops
When critical issues appear:
1. Validation issues are summarized.
2. The wiring/netlist agent is re-run with the error report.
3. Nets are regenerated and validation re-runs.

This loop keeps the IR grounded in real-world electrical constraints.

## Safety boundaries
Forma is intentionally constrained to **low-voltage maker electronics**. The system blocks or warns on:
- Mains AC systems (110–240V)
- Medical or life-support devices
- Automotive control systems
- Weapons or hazardous systems
- High-power battery packs

These checks are enforced **before** the agent pipeline runs (`check_safety_violations`) to keep the MVP safe and focused.

## Standalone validation endpoint
You can validate arbitrary parts + nets without running the full pipeline:
- `POST /api/validate`
