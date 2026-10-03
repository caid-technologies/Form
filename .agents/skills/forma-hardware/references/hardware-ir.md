# Agent-authored Hardware Intermediate Representation

Author a concrete, internally consistent project. Use unique reference designators and net IDs. Every net endpoint must point to a declared component pin.

Minimum useful shape:

```json
{
  "hardware_ir_version": "0.2",
  "overview": {
    "title": "Project title",
    "description": "Purpose and safe scope",
    "difficulty": "Beginner",
    "estimated_cost": 0,
    "category": "IoT"
  },
  "requirements": {
    "requirements": ["Measurable functional requirement"],
    "power_needs": "5V USB",
    "operating_voltage": 3.3,
    "physical_constraints": [],
    "safety_notes": ["Disconnect power before rewiring."],
    "missing_info": []
  },
  "components": [
    {
      "ref_des": "U1",
      "part_number": "ESP32-DEVKIT",
      "name": "ESP32 development board",
      "category": "Microcontroller",
      "quantity": 1,
      "unit_price": 9.5,
      "sourcing_url": null,
      "rationale": "Controls the prototype.",
      "pins": [
        {"pin_id": "3V3", "name": "3V3", "pin_type": "Power", "voltage": 3.3, "description": "Regulated output"},
        {"pin_id": "GND", "name": "GND", "pin_type": "Ground", "voltage": 0, "description": "Ground"}
      ]
    }
  ],
  "nets": [
    {
      "net_id": "NET_3V3",
      "name": "3.3V rail",
      "net_type": "Power",
      "voltage": 3.3,
      "pins": [{"ref_des": "U1", "pin_id": "3V3"}]
    }
  ],
  "buses": [],
  "pin_mappings": [],
  "assembly": [],
  "constraints": ["Low-voltage DC only"],
  "power_rails": [],
  "estimated_current_draw_ma": 250,
  "fabrication_notes": [],
  "assembly_metadata": {},
  "project_version_history": [],
  "validation": {"critical": [], "warning": [], "info": []},
  "is_valid": true
}
```

Use pin types such as `Power`, `Ground`, `Digital`, `Analog`, `I2C`, `SPI`, `UART`, `PWM`, or `Passive`.

Before compiling, check:

- Each physical component is a separate instance with a unique reference designator.
- Every `ref_des.pin_id` endpoint exists in `components[].pins`.
- Power and signal voltages are compatible.
- Quantities, prices, and rationale are present when known; label estimates.
- Assembly instructions cover disconnected wiring, polarity inspection, current-limited first power-up, and testing.
- Mechanical dimensions and clearances are labeled estimates unless verified against CAD or measured parts.
