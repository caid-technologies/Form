"""Map pinned component files into canonical project revision artifacts."""
from forma_core.workspaces.projects.state import ProjectArtifact

def pin_artifacts(pin: dict) -> list[ProjectArtifact]:
    return [ProjectArtifact(
        artifact_id=f"component-asset-{pin['instance_id']}-{rep['name']}", kind="component.cad",
        uri=f"forma://component-assets/{pin['asset_id']}/{pin['version']}/{rep['name']}",
        media_type=rep["media_type"], checksum="sha256:" + rep["sha256"],
        metadata={"asset_id": pin["asset_id"], "version": pin["version"], "instance_id": pin["instance_id"],
                  "representation": rep["name"], "position_mm": pin["position_mm"], "rotation_deg": pin["rotation_deg"]},
    ) for rep in pin["representations"]]



def component_pin_artifacts(state):
    pins = (state.assembly_metadata or {}).get("component_asset_refs") or {}
    return [artifact for pin in pins.values() for artifact in pin_artifacts(pin)]
