from typing import List, Dict, Set, Optional
from forma_core.workspaces.projects.models import (
    ComponentInstance,
    ConnectionNet,
    FunctionalRequirements,
    PinDefinition,
    ValidationIssue,
    ValidationSummary,
)


REQUIREMENT_CAPABILITIES = (
    ("soil-moisture sensing", ("soil moisture", "soil-moisture"), ("soil moisture", "soil-moisture", "capacitive moisture")),
    ("ambient-light sensing", ("ambient light", "light sensing", "light sensor", "lux"), ("ambient light", "light sensor", "photoresistor", "ldr", "bh1750", "lux sensor")),
    ("temperature sensing", ("temperature",), ("temperature", "dht11", "dht22", "bmp280", "thermistor")),
    ("humidity sensing", ("humidity",), ("humidity", "dht11", "dht22", "bme280")),
    ("distance sensing", ("distance", "proximity", "ultrasonic"), ("distance", "ultrasonic", "hc-sr04", "vl53")),
    ("motion sensing", ("motion sensing", "accelerometer", "gyroscope", "imu"), ("motion", "accelerometer", "gyroscope", "mpu6050", "imu")),
    ("pressure sensing", ("barometric", "air pressure", "pressure sensor"), ("barometric", "pressure", "bmp280", "bme280")),
    ("visual display", ("display", "oled", "lcd", "screen"), ("display", "oled", "lcd", "ssd1306")),
    ("audible alert", ("buzzer", "audible alert", "beeper", "speaker"), ("buzzer", "piezo", "speaker", "beeper")),
    ("status LED", ("status led", "indicator led"), (" led", "led-", "status indicator")),
    ("220-ohm current limiting", ("220-ohm", "220 ohm", "220r"), ("220 ohm", "220r", "resistor-220")),
    ("10k pull-up resistance", ("10k pull-up", "10k pull up", "10k resistor"), ("10k ohm", "resistor-10k")),
    ("servo actuation", ("servo",), ("servo", "sg90")),
    ("relay switching", ("relay",), ("relay",)),
    ("USB-C connectivity", ("usb-c", "usb c", "type-c", "type c"), ("usb-c", "usb c", "type-c", "type c")),
)


def validate_requirement_coverage(
    requirements: Optional[FunctionalRequirements],
    components: List[ComponentInstance],
    *,
    prompt: str = "",
) -> List[ValidationIssue]:
    """Report requested hardware capabilities that have no matching BOM component."""
    if requirements is None and not prompt.strip():
        return []
    requirement_text = " ".join(
        [prompt, *((requirements.requirements if requirements else []) or [])]
    ).lower()
    component_text = " ".join(
        f" {component.part_number} {component.name} {component.category} {component.rationale} "
        for component in components
    ).lower()
    issues: List[ValidationIssue] = []
    for label, request_phrases, component_phrases in REQUIREMENT_CAPABILITIES:
        if not any(phrase in requirement_text for phrase in request_phrases):
            continue
        if any(phrase in component_text for phrase in component_phrases):
            continue
        issues.append(ValidationIssue(
            severity="WARNING",
            category="Requirement Coverage",
            description=f"The project requests {label}, but the selected BOM has no component that provides it.",
            troubleshooting=(
                f"Add a catalog component that provides {label}, or explicitly mark this requirement as unsupported "
                "instead of presenting the design as complete."
            ),
        ))
    return issues


def check_safety_violations(prompt: str) -> Optional[str]:
    """
    Checks if the user's prompt contains safety violations that fall outside MVP scope.
    Specifically blocks/warns on: mains AC systems, medical devices, automotive systems, weapons, high-power battery systems.
    """
    prompt_lower = prompt.lower()
    
    # 1. Weapons
    weapon_keywords = ["weapon", "gun", "firearm", "missile", "explosive", "grenade", "bomb", "defense system", "tactical military", "ammunition", "artillery", "pistol"]
    for word in weapon_keywords:
        if word in prompt_lower:
            return f"Safety Block: Weapons-related projects ('{word}') are strictly blocked. Forma only supports educational, hobbyist, and safe IoT hardware prototypes."
            
    # 2. Medical Devices
    medical_keywords = ["medical", "pacemaker", "ventilator", "life support", "implant", "clinical health", "surgical", "life-support", "dialysis", "biomedical"]
    for word in medical_keywords:
        if word in prompt_lower:
            return f"Safety Block: Critical medical or life-support devices ('{word}') are strictly blocked. Forma only generates low-voltage educational prototypes and does not compile medical grade electronics."

    # 3. Automotive Systems
    automotive_keywords = ["automotive", "car system", "ecu control", "engine control", "vehicle safety", "brake control", "can-bus car", "throttle control", "autopilot car"]
    for word in automotive_keywords:
        if word in prompt_lower:
            return f"Safety Block: High-risk automotive vehicle control systems ('{word}') are blocked to prevent unsafe driving automation prototypes."

    # 4. Mains AC
    mains_keywords = ["mains ac", "110v", "220v", "ac mains", "outlet power", "wall plug ac", "high voltage ac", "240v", "ac outlet", "wall socket"]
    for word in mains_keywords:
        if word in prompt_lower:
            return f"Safety Warning: Projects switching mains AC electricity (110V-240V) are explicitly blocked. Please modify your prompt to use low-voltage DC relays (e.g. switching 5V or 12V DC elements) for electrical safety."

    # 5. High-Power Batteries
    battery_keywords = ["high-power battery", "high power battery", "tesla pack", "48v battery", "60v battery", "high voltage lithium", "ev battery", "electric vehicle battery"]
    for word in battery_keywords:
        if word in prompt_lower:
            return f"Safety Warning: High-power battery packs and electric vehicle charging systems are blocked due to extreme fire and electrical hazards. Please focus on low-voltage battery setups (such as standard 3.7V LiPo or AA cells)."

    return None

def validate_circuit(
    components: List[ComponentInstance],
    nets: List[ConnectionNet],
    requirements: Optional[FunctionalRequirements] = None,
    *,
    prompt: str = "",
) -> List[ValidationIssue]:
    """
    Runs automated electrical and logical validation checks on the structured Hardware Intermediate Representation netlist.
    Returns a list of ValidationIssues (Errors and Warnings) with troubleshooting advice.
    """
    issues: List[ValidationIssue] = validate_requirement_coverage(requirements, components, prompt=prompt)

    # Mechanical BOM items have no electrical terminals. Unknown categories are
    # not an exemption: an unmodelled module cannot be electrically verified.
    mechanical_categories = {"mechanical", "enclosure", "fastener", "fasteners", "structural", "3d print"}
    electrical_components = [
        comp for comp in components
        if comp.pins or comp.category.strip().lower() not in mechanical_categories
    ]
    for comp in electrical_components:
        if not comp.pins:
            issues.append(ValidationIssue(
                severity="CRITICAL",
                category="Missing Pin Definitions",
                description=f"Component '{comp.name}' ({comp.ref_des}) has no declared electrical pins and cannot be validated.",
                troubleshooting="Declare the part's physical terminals, including module power and interface connections. Use a mechanical category only for non-electrical parts.",
            ))
    if electrical_components and not nets:
        issues.append(ValidationIssue(
            severity="CRITICAL",
            category="Missing Electrical Nets",
            description="The project contains electrical components but no electrical netlist.",
            troubleshooting="Define the actual power, ground, and signal connections. Power-rail descriptions and assembly instructions do not replace a netlist.",
        ))
    
    # Pre-index component pin attributes for fast lookup
    # key: (ref_des, pin_id) -> PinDefinition
    pin_lookup: Dict[tuple, PinDefinition] = {}
    component_lookup: Dict[str, ComponentInstance] = {}
    
    for comp in components:
        component_lookup[comp.ref_des] = comp
        for pin in comp.pins:
            pin_lookup[(comp.ref_des, pin.pin_id)] = pin

    # Reject malformed endpoints before applying electrical rules. This keeps
    # provider hallucinations and duplicate references visible instead of
    # silently omitting them from the pin lookup below.
    for net in nets:
        if not net.pins:
            issues.append(ValidationIssue(
                severity="CRITICAL",
                category="Empty Net",
                description=f"Net '{net.name}' ({net.net_id}) contains no endpoints.",
                troubleshooting="Add at least one declared component pin or remove the empty net.",
            ))
        seen_endpoints: set[tuple[str, str]] = set()
        for pin_ref in net.pins:
            endpoint = (pin_ref.ref_des, pin_ref.pin_id)
            if endpoint in seen_endpoints:
                issues.append(ValidationIssue(
                    severity="CRITICAL",
                    category="Duplicate Endpoint",
                    description=(
                        f"Endpoint '{pin_ref.ref_des}.{pin_ref.pin_id}' appears more than once "
                        f"in net '{net.net_id}'."
                    ),
                    troubleshooting="List each physical endpoint at most once per net.",
                ))
            seen_endpoints.add(endpoint)
            if pin_ref.ref_des not in component_lookup:
                issues.append(ValidationIssue(
                    severity="CRITICAL",
                    category="Unknown Component Reference",
                    description=(
                        f"Net '{net.net_id}' references unknown component "
                        f"'{pin_ref.ref_des}'."
                    ),
                    troubleshooting="Choose a component reference declared in the project BOM.",
                ))
            elif endpoint not in pin_lookup:
                issues.append(ValidationIssue(
                    severity="CRITICAL",
                    category="Unknown Pin Reference",
                    description=(
                        f"Net '{net.net_id}' references undeclared pin "
                        f"'{pin_ref.pin_id}' on component '{pin_ref.ref_des}'."
                    ),
                    troubleshooting="Choose a pin declared by the component's shared part definition.",
                ))

    # Pin to Nets reverse lookup to find pin conflict issues
    # key: (ref_des, pin_id) -> List of net_ids
    pin_to_nets: Dict[tuple, List[str]] = {}
    for net in nets:
        for pin_ref in net.pins:
            key = (pin_ref.ref_des, pin_ref.pin_id)
            if key not in pin_to_nets:
                pin_to_nets[key] = []
            pin_to_nets[key].append(net.net_id)

    # ----------------------------------------------------
    # Rule 1: Short Circuit Checker (Power directly to Ground)
    # ----------------------------------------------------
    for net in nets:
        has_power = False
        has_ground = False
        power_pins = []
        ground_pins = []
        
        for pin_ref in net.pins:
            pin = pin_lookup.get((pin_ref.ref_des, pin_ref.pin_id))
            if pin:
                if pin.pin_type.lower() == "power":
                    has_power = True
                    power_pins.append(f"{pin_ref.ref_des}.{pin_ref.pin_id}")
                elif pin.pin_type.lower() == "ground":
                    has_ground = True
                    ground_pins.append(f"{pin_ref.ref_des}.{pin_ref.pin_id}")
                    
        if has_power and has_ground:
            issues.append(ValidationIssue(
                severity="CRITICAL",
                category="Short Circuit",
                description=f"Direct electrical short detected in net '{net.name}' ({net.net_id}). "
                            f"Power pins [{', '.join(power_pins)}] are connected directly to Ground pins [{', '.join(ground_pins)}].",
                troubleshooting="Separate the power rail connections from the ground reference rail. Power pins must only connect to other power nodes, never directly to GND."
            ))

    # ----------------------------------------------------
    # Rule 2: Voltage Mismatch Checker
    # ----------------------------------------------------
    for net in nets:
        voltages: Set[float] = set()
        connected_pins = []
        
        for pin_ref in net.pins:
            pin = pin_lookup.get((pin_ref.ref_des, pin_ref.pin_id))
            if pin and pin.voltage is not None:
                # Store pin name and operating voltage
                voltages.add(pin.voltage)
                connected_pins.append(f"{pin_ref.ref_des}.{pin_ref.pin_id} ({pin.voltage}V)")
                
        # If we have multiple different positive voltages on the same signal rail
        if len(voltages) > 1:
            max_v = max(voltages)
            min_v = min(voltages)
            # If the difference is significant (e.g. 5.0V and 3.3V on the same net)
            if max_v - min_v > 0.5:
                issues.append(ValidationIssue(
                    severity="WARNING",
                    category="Voltage Mismatch",
                    description=f"Potential voltage mismatch in net '{net.name}' ({net.net_id}). "
                                f"Pins with different voltages are connected on the same net: {', '.join(connected_pins)}.",
                    troubleshooting=f"Use an active level-shifter (e.g., TXB0104) to bridge logic between {min_v}V and {max_v}V lines, or use a component operating at compatible voltages."
                ))

    # ----------------------------------------------------
    # Rule 3: Floating / Unpowered IC Check
    # ----------------------------------------------------
    # Identify which components are active ICs (MCUs, Sensors, Actuators, Displays)
    for ref_des, comp in component_lookup.items():
        if comp.category.lower() in ["microcontroller", "sensor", "display", "actuator"]:
            # Check if this component has power & ground pins and if they are connected
            has_power_pin = False
            has_ground_pin = False
            power_connected = False
            ground_connected = False
            
            p_pin_ids = []
            g_pin_ids = []
            
            for pin in comp.pins:
                if pin.pin_type.lower() == "power":
                    has_power_pin = True
                    p_pin_ids.append(pin.pin_id)
                    # Check if this specific pin is in any net
                    if (ref_des, pin.pin_id) in pin_to_nets:
                        power_connected = True
                elif pin.pin_type.lower() == "ground":
                    has_ground_pin = True
                    g_pin_ids.append(pin.pin_id)
                    if (ref_des, pin.pin_id) in pin_to_nets:
                        ground_connected = True
                        
            # If it has pins but they're not connected
            if has_power_pin and not power_connected:
                issues.append(ValidationIssue(
                    severity="CRITICAL",
                    category="Unpowered IC",
                    description=f"Active component '{comp.name}' ({ref_des}) is unpowered. "
                                f"None of its power pins [{', '.join(p_pin_ids)}] are connected to an active power net.",
                    troubleshooting=f"Connect one of the VCC/Power pins on {ref_des} to the main power rail (e.g., 3.3V or 5V net)."
                ))
            if has_ground_pin and not ground_connected:
                issues.append(ValidationIssue(
                    severity="CRITICAL",
                    category="Unpowered IC",
                    description=f"Active component '{comp.name}' ({ref_des}) has no ground reference. "
                                f"None of its ground pins [{', '.join(g_pin_ids)}] are tied to the GND net.",
                    troubleshooting=f"Connect the GND/Ground pin on {ref_des} to the common system Ground net (GND)."
                ))

    # ----------------------------------------------------
    # Rule 4: Pin Reuse Confli
    # ----------------------------------------------------
    for (ref_des, pin_id), net_ids in pin_to_nets.items():
        # Exclude passive/power/ground buses which naturally share pins
        pin = pin_lookup.get((ref_des, pin_id))
        if pin and pin.pin_type.lower() not in ["power", "ground", "passive"]:
            if len(net_ids) > 1:
                comp = component_lookup.get(ref_des)
                comp_name = comp.name if comp else ref_des
                issues.append(ValidationIssue(
                    severity="CRITICAL",
                    category="Pin Conflict",
                    description=f"Pin reuse conflict detected! Pin '{pin_id}' on '{comp_name}' ({ref_des}) "
                                f"is connected to multiple independent signal nets: {', '.join(net_ids)}.",
                    troubleshooting=f"Reassign pin '{pin_id}' to only belong to a single signal net. Signal pins cannot be shared directly across separate signal/communication lines."
                ))

    # ----------------------------------------------------
    # Rule 5: Over-Current Warn Check (Power-Hungry Actuators)
    # ----------------------------------------------------
    has_mcu = False
    mcu_ref = None
    high_draw_actuator_refs: Dict[str, str] = {}
    
    for ref_des, comp in component_lookup.items():
        if comp.category.lower() == "microcontroller":
            has_mcu = True
            mcu_ref = ref_des
        else:
            component_text = f"{comp.name} {comp.part_number}".lower()
            is_high_draw_actuator = (
                comp.part_number in ["Relay-5V-1Ch", "SG90-Servo"]
                or any(keyword in component_text for keyword in ["relay", "servo", "motor", "pump"])
            )
            if is_high_draw_actuator:
                high_draw_actuator_refs[ref_des] = f"{comp.name} ({ref_des})"
            
    if has_mcu and high_draw_actuator_refs:
        for net in nets:
            if net.net_type.lower() != "power" or net.voltage != 3.3:
                continue

            contains_mcu_power_pin = False
            powered_actuators = []
            for pin_ref in net.pins:
                pin = pin_lookup.get((pin_ref.ref_des, pin_ref.pin_id))
                if not pin or pin.pin_type.lower() != "power":
                    continue
                if pin_ref.ref_des == mcu_ref:
                    contains_mcu_power_pin = True
                elif pin_ref.ref_des in high_draw_actuator_refs:
                    powered_actuators.append(high_draw_actuator_refs[pin_ref.ref_des])
            
            if contains_mcu_power_pin and powered_actuators:
                issues.append(ValidationIssue(
                    severity="WARNING",
                    category="Overcurrent Risk",
                    description=f"High-power actuator(s) [{', '.join(powered_actuators)}] are powered from the same 3.3V low-current output "
                                f"net '{net.name}' as the MCU ({mcu_ref}). Relays and servo motors draw peak currents that can crash the microcontroller or burn out its internal voltage regulator.",
                    troubleshooting="Isolate the actuator power. Connect the servo/relay power pin to a dedicated 5V input rail or external power source, sharing only the ground reference (GND) with the MCU."
                ))

    return issues

def build_validation_summary(issues: List[ValidationIssue]) -> ValidationSummary:
    """
    Groups a list of individual ValidationIssue models into critical, warning, and info lists.
    """
    critical = [issue for issue in issues if issue.severity.upper() == "CRITICAL"]
    warning = [issue for issue in issues if issue.severity.upper() == "WARNING"]
    info = [issue for issue in issues if issue.severity.upper() == "INFO"]
    return ValidationSummary(critical=critical, warning=warning, info=info)
