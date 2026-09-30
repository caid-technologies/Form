import re
from typing import Dict
import html
from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation

_MERMAID_ID_PATTERN = re.compile(r"[^a-zA-Z0-9_]")


def _mermaid_text(value: object) -> str:
    return html.escape(str(value or ""), quote=True).replace("\n", " ")


def _mermaid_node_id(value: object, fallback: str) -> str:
    node_id = _MERMAID_ID_PATTERN.sub("_", str(value or fallback)).strip("_")
    if not node_id:
        node_id = fallback
    if node_id[0].isdigit():
        node_id = f"N_{node_id}"
    return node_id


def _build_mermaid_node_ids(components: list) -> Dict[str, str]:
    seen: Dict[str, int] = {}
    node_ids: Dict[str, str] = {}

    for index, comp in enumerate(components, start=1):
        base_id = _mermaid_node_id(comp.ref_des, f"COMP_{index}")
        count = seen.get(base_id, 0)
        seen[base_id] = count + 1
        node_ids[comp.ref_des] = base_id if count == 0 else f"{base_id}_{count + 1}"

    return node_ids


def _mermaid_component_ref(node_ids: Dict[str, str], ref_des: str) -> str:
    return node_ids.get(ref_des, _mermaid_node_id(ref_des, "UNKNOWN"))


def generate_mermaid_chart(ir: HardwareIntermediateRepresentation) -> str:
    """
    Generates a valid Mermaid.js flowchart string mapping all electrical connections
    between components for direct display on the client.
    """
    if not ir or not ir.components:
        return 'graph TD\n  Empty["No Components Instantiated"]'

    lines = ["graph LR"]
    node_ids = _build_mermaid_node_ids(ir.components)
    
    # Define subgraphs/styling or component nodes
    for comp in ir.components:
        label = (
            f"<b>{_mermaid_text(comp.ref_des)}</b><br/>"
            f"{_mermaid_text(comp.name)}<br/>"
            f"<small>{_mermaid_text(comp.part_number)}</small>"
        )
        lines.append(f'  {node_ids[comp.ref_des]}["{label}"]')

    # Build connection lines grouped by net
    # For Mermaid, draw lines from the source (usually MCU or power supply) to targets
    mcu_refs = [c.ref_des for c in ir.components if c.category.lower() == "microcontroller"]
    mcu_ref = mcu_refs[0] if mcu_refs else None

    # Track drawn connections to prevent duplicates
    drawn_connections = set()

    for net in ir.nets:
        # Ignore GND and main Power rails in signal wiring diagram to keep it clean, or represent them minimally
        if net.net_id in ["NET_GND", "NET_VCC", "NET_3V3", "NET_5V"]:
            continue
            
        # If we have an MCU on the net, draw connections outward from MCU
        mcu_pins_in_net = [p for p in net.pins if p.ref_des == mcu_ref]
        other_pins_in_net = [p for p in net.pins if p.ref_des != mcu_ref]
        
        if mcu_pins_in_net and other_pins_in_net:
            src = mcu_pins_in_net[0]
            for dest in other_pins_in_net:
                conn_key = (src.ref_des, dest.ref_des, net.net_id)
                if conn_key not in drawn_connections:
                    drawn_connections.add(conn_key)
                    label = _mermaid_text(f"{net.name} ({src.pin_id} -> {dest.pin_id})")
                    src_node = _mermaid_component_ref(node_ids, src.ref_des)
                    dest_node = _mermaid_component_ref(node_ids, dest.ref_des)
                    lines.append(f'  {src_node} -->|"{label}"| {dest_node}')
        else:
            # Connect components sequentially if no MCU is present
            for i in range(len(net.pins) - 1):
                src = net.pins[i]
                dest = net.pins[i+1]
                conn_key = (src.ref_des, dest.ref_des, net.net_id)
                if conn_key not in drawn_connections:
                    drawn_connections.add(conn_key)
                    label = _mermaid_text(f"{net.name} ({src.pin_id} <-> {dest.pin_id})")
                    src_node = _mermaid_component_ref(node_ids, src.ref_des)
                    dest_node = _mermaid_component_ref(node_ids, dest.ref_des)
                    lines.append(f'  {src_node} -.->|"{label}"| {dest_node}')

    return "\n".join(lines)


def generate_svg_schematic(ir: HardwareIntermediateRepresentation) -> str:
    """
    Generates a beautifully arranged, color-coded SVG schematic of the circuit.
    Renders MCUs in the center, sensors on the left, displays/actuators on the right.
    Wires are drawn as orthogonal bezier curves.
    """
    if not ir or not ir.components:
        return "<svg width='400' height='200'><text x='20' y='50'>No circuit defined</text></svg>"

    # Dimensions
    width = 1000
    height = 600
    
    # Divide layout columns
    # Left column: Sensors & Power (x=150)
    # Center column: Microcontroller (x=500)
    # Right column: Actuators & Displays (x=850)
    
    # Layout positioning maps
    # ref_des -> (x, y)
    coords: Dict[str, tuple] = {}
    
    mcu_parts = [c for c in ir.components if c.category.lower() == "microcontroller"]
    sensor_parts = [c for c in ir.components if c.category.lower() == "sensor" or c.category.lower() == "power"]
    output_parts = [c for c in ir.components if c.category.lower() in ["actuator", "display", "passives"]]
    
    # Place MCU in center
    if mcu_parts:
        for idx, mcu in enumerate(mcu_parts):
            coords[mcu.ref_des] = (500, 200 + (idx * 220))
            
    # Place Sensors on left
    for idx, sens in enumerate(sensor_parts):
        coords[sens.ref_des] = (150, 100 + (idx * 140))
        
    # Place Outputs on right
    for idx, out in enumerate(output_parts):
        coords[out.ref_des] = (850, 100 + (idx * 140))
        
    # Standard fallback coordinates for any remaining parts
    for comp in ir.components:
        if comp.ref_des not in coords:
            coords[comp.ref_des] = (500, 450)

    # Begin building SVG
    svg_elements = [
        f"<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 {width} {height}' width='100%' height='100%'>",
        "  <!-- Grid Background -->",
        "  <defs>",
        "    <pattern id='grid' width='20' height='20' patternUnits='userSpaceOnUse'>",
        "      <path d='M 20 0 L 0 0 0 20' fill='none' stroke='#f0f2f5' stroke-width='1'/>",
        "    </pattern>",
        "  </defs>",
        "  <rect width='100%' height='100%' fill='url(#grid)' />",
        "  <rect width='100%' height='100%' fill='none' stroke='#cbd5e1' stroke-width='2' />",
        "  <text x='25' y='35' font-family='sans-serif' font-size='18' font-weight='bold' fill='#0f172a'>Electrical Assembly Schematic</text>",
        f"  <text x='25' y='55' font-family='sans-serif' font-size='12' fill='#64748b'>{html.escape(ir.overview.title if ir.overview else 'Hardware Project')}</text>"
    ]

    # Render wires (Nets) first so they are layered behind component boxes
    svg_elements.append("  <!-- Wires / Connections -->")
    
    net_colors = {
        "ground": "#000000",       # GND is Black
        "power": "#dc2626",        # VCC is Red
        "i2c": "#06b6d4",          # I2C is Cyan
        "spi": "#a855f7",          # SPI is Purple
        "digital": "#3b82f6",      # GPIO is Blue
        "analog": "#f59e0b",       # Analog is Amber
        "pwm": "#10b981",          # PWM is Emerald
        "default": "#64748b"       # General signal is Slate
    }

    drawn_lines = set()
    for net in ir.nets:
        color = net_colors.get(net.net_type.lower(), net_colors["default"])
        stroke_width = 2.5 if net.net_type.lower() in ["power", "ground"] else 1.5
        dash_array = "4 4" if net.net_type.lower() == "ground" else "none"
        
        # Draw connections
        # Connect all pins in a net to a central hub, or chain them together
        if len(net.pins) >= 2:
            # Let's chain them sequentially
            for i in range(len(net.pins) - 1):
                p1 = net.pins[i]
                p2 = net.pins[i+1]
                
                pt1 = coords.get(p1.ref_des)
                pt2 = coords.get(p2.ref_des)
                
                if pt1 and pt2:
                    # Offsets to connect on left/right side of components
                    x1, y1 = pt1
                    x2, y2 = pt2
                    
                    # Offset depending on column
                    x1_offset = 110 if x1 < 400 else (-110 if x1 > 600 else (110 if x2 > x1 else -110))
                    x2_offset = -110 if x2 > 600 else (110 if x2 < 400 else (110 if x1 > x2 else -110))
                    
                    cx1, cy1 = x1 + x1_offset, y1
                    cx2, cy2 = x2 + x2_offset, y2
                    
                    line_key = tuple(sorted([(cx1, cy1), (cx2, cy2)]))
                    if line_key not in drawn_lines:
                        drawn_lines.add(line_key)
                        
                        # Draw Bezier Curve for organic circuit routing look
                        control_x1 = cx1 + (cx2 - cx1) * 0.4
                        control_x2 = cx1 + (cx2 - cx1) * 0.6
                        
                        path_d = f"M {cx1} {cy1} C {control_x1} {cy1}, {control_x2} {cy2}, {cx2} {cy2}"
                        svg_elements.append(
                            f"  <path d='{path_d}' fill='none' stroke='{color}' stroke-width='{stroke_width}' "
                            f"stroke-dasharray='{dash_array}' opacity='0.85' />"
                        )

    # Render Components
    svg_elements.append("  <!-- Components -->")
    for comp in ir.components:
        x, y = coords[comp.ref_des]
        
        # Component Box Dimensions
        c_width = 220
        c_height = 120
        box_x = x - (c_width // 2)
        box_y = y - (c_height // 2)
        
        # Category Colors
        cat_styles = {
            "microcontroller": {"fill": "#eff6ff", "border": "#2563eb", "badge_fill": "#dbeafe", "badge_text": "#1e40af"},
            "sensor": {"fill": "#f0fdf4", "border": "#16a34a", "badge_fill": "#dcfce7", "badge_text": "#14532d"},
            "display": {"fill": "#faf5ff", "border": "#7c3aed", "badge_fill": "#f3e8ff", "badge_text": "#581c87"},
            "actuator": {"fill": "#fffbeb", "border": "#d97706", "badge_fill": "#fef3c7", "badge_text": "#78350f"},
            "power": {"fill": "#fef2f2", "border": "#dc2626", "badge_fill": "#fee2e2", "badge_text": "#7f1d1d"},
            "default": {"fill": "#f8fafc", "border": "#475569", "badge_fill": "#e2e8f0", "badge_text": "#1e293b"}
        }
        
        style = cat_styles.get(comp.category.lower(), cat_styles["default"])
        
        # Render Component Rectangle Card
        svg_elements.append(f"  <g id='comp-{comp.ref_des}'>")
        svg_elements.append(
            f"    <rect x='{box_x}' y='{box_y}' width='{c_width}' height='{c_height}' rx='8' ry='8' "
            f"fill='{style['fill']}' stroke='{style['border']}' stroke-width='2' filter='drop-shadow(0 2px 4px rgba(0,0,0,0.05))'/>"
        )
        
        # Component Badge (Category)
        svg_elements.append(
            f"    <rect x='{box_x + 12}' y='{box_y + 12}' width='100' height='18' rx='4' ry='4' fill='{style['badge_fill']}' />"
        )
        svg_elements.append(
            f"    <text x='{box_x + 62}' y='{box_y + 25}' font-family='sans-serif' font-size='9' font-weight='bold' "
            f"fill='{style['badge_text']}' text-anchor='middle'>{comp.category.upper()}</text>"
        )
        
        # Reference Designator
        svg_elements.append(
            f"    <text x='{box_x + c_width - 15}' y='{box_y + 26}' font-family='sans-serif' font-size='14' font-weight='bold' "
            f"fill='#0f172a' text-anchor='end'>{comp.ref_des}</text>"
        )
        
        # Part Name
        name_truncated = comp.name if len(comp.name) <= 24 else comp.name[:21] + "..."
        svg_elements.append(
            f"    <text x='{box_x + 12}' y='{box_y + 55}' font-family='sans-serif' font-size='12' font-weight='bold' fill='#0f172a'>{html.escape(name_truncated)}</text>"
        )
        
        # Part Number
        svg_elements.append(
            f"    <text x='{box_x + 12}' y='{box_y + 75}' font-family='sans-serif' font-size='10' fill='#64748b'>{comp.part_number}</text>"
        )
        
        # Pin Count Badge / Sourcing Pricing
        svg_elements.append(
            f"    <text x='{box_x + 12}' y='{box_y + 102}' font-family='sans-serif' font-size='10' font-weight='600' fill='#475569'>Est: ${comp.unit_price:.2f}</text>"
        )
        svg_elements.append(
            f"    <text x='{box_x + c_width - 12}' y='{box_y + 102}' font-family='sans-serif' font-size='10' fill='#64748b' text-anchor='end'>{len(comp.pins)} Physical Pins</text>"
        )
        
        # Draw physical connection hubs (ports) on Left & Right bounds of component card
        # Left Port
        svg_elements.append(f"    <circle cx='{box_x}' cy='{y}' r='5' fill='#ffffff' stroke='{style['border']}' stroke-width='2' />")
        # Right Port
        svg_elements.append(f"    <circle cx='{box_x + c_width}' cy='{y}' r='5' fill='#ffffff' stroke='{style['border']}' stroke-width='2' />")
        
        svg_elements.append("  </g>")

    svg_elements.append("</svg>")
    return "\n".join(svg_elements)
