"""Build real STEP geometry and a motion preview, reusing one pinned SG90 asset.

Run from the repository: python examples/sg90-library-arm/build.py --output /path/to/output
Optional dependencies: cadquery==2.6.1 pyvista==0.46.5 imageio==2.37.0
"""
from __future__ import annotations
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

import cadquery as cq
import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def tr(x=0, y=0, z=0):
    m = np.eye(4); m[:3,3] = (x,y,z); return m


def rot(axis, angle):
    a = math.radians(angle); c,s = math.cos(a), math.sin(a)
    m = np.eye(4)
    if axis == "x": m[:3,:3] = [[1,0,0],[0,c,-s],[0,s,c]]
    if axis == "y": m[:3,:3] = [[c,0,s],[0,1,0],[-s,0,c]]
    if axis == "z": m[:3,:3] = [[c,-s,0],[s,c,0],[0,0,1]]
    return m


def box(x,y,z,c=(0,0,0)):
    return cq.Workplane("XY").box(x,y,z).translate(c).val()


def cyl(r,h,c=(0,0,0)):
    return cq.Workplane("XY").circle(r).extrude(h).translate(c).val()


def servo():
    # Output axis Z; supplied horn is a separate moving assembly component.
    body=box(23,12.2,22,(5.2,0,-16))
    ears=box(32.3,12.2,2.5,(5.2,0,-8))
    for x in (-8.9,19.3): ears=ears.cut(cyl(1.1,5,(x,0,-10)))
    return body.fuse(ears,cyl(5.7,3,(0,0,-5)),cyl(2.4,4,(0,0,-2)))


def link(length):
    # A light cheek plate in XY, later oriented into the arm's XZ plane.
    plate=box(length,14,2.8,(length/2,0,0))
    for x in (0,length): plate=plate.fuse(cyl(10,2.8,(x,0,-1.4)))
    for x in (0,length): plate=plate.cut(cyl(2,6,(x,0,-3)))
    for x in np.linspace(15,length-15,3): plate=plate.cut(cyl(3.8,6,(float(x),0,-3)))
    return plate


def make_parts(servo_shape):
    parts=[]
    def add(name, shape, color, group="static", local=None, printed=False):
        parts.append(dict(name=name,shape=shape,color=color,group=group,
                          local=np.eye(4) if local is None else local,printed=printed))
    ivory="#dbe3e9"; navy="#243d52"; blue="#155bc5"; metal="#abbac5"; orange="#fbad5d"
    base=box(94,82,6,(0,0,3))
    for x in (-39,39):
        for y in (-33,33): base=base.cut(cyl(2.2,8,(x,y,-1)))
    # Base cavity keeps the stationary yaw servo accessible from below.
    pedestal=box(40,27,21.75,(5.2,0,16.875)).cut(box(23.8,13,40,(5.2,0,21)))
    pedestal=pedestal.cut(box(23.8,12,15,(5.2,-12,17)))
    add("base",base,navy,printed=True)
    add("yaw-servo-cradle",pedestal,ivory,printed=True)
    add("S1-base-SG90",servo_shape,blue,local=tr(0,0,37))
    add("turntable",cyl(25,3,(0,0,40)).cut(cyl(3,5,(0,0,39))),ivory,"yaw",printed=True)
    # Shoulder's tabs bear on a vertical plate. Use the supplier horn at the output.
    side=box(36,3,36,(5,-22.75,63)).cut(box(24,5,13,(5,-22.75,63)))
    for x in (-8.9,19.3):
        hole=cyl(1.2,8,(0,0,0)).rotate((0,0,0),(1,0,0),90).translate((x,-19,65))
        side=side.cut(hole)
    add("shoulder-mount",side,ivory,"yaw",printed=True)
    add("shoulder-idler-post",box(16,3,29,(0,12,55)),ivory,"yaw",printed=True)
    add("S2-shoulder-SG90",servo_shape,blue,"shoulder_case",tr(0,-12,0)@rot("x",-90))
    # Two independent plates leave room for wiring and the opposing pivot.
    for group,length in (("upper",55),("fore",45)):
        for side_y in (-7,7):
            add(f"{group}-cheek-{side_y}",link(length),ivory,group,tr(0,side_y,0)@rot("x",90),True)
        for x in (12,length-12):
            add(f"{group}-spacer-{x}",cyl(2.6,14),metal,group,tr(x,7,0)@rot("x",90))
        add(f"{group}-idler",cyl(3.6,5),orange,group,tr(0,9,0)@rot("x",-90))
    elbow_mount=box(34,3,17,(55+5.2,-22.75,0)).cut(box(23.8,6,12.8,(55+5.2,-22.75,0)))
    add("elbow-cradle",elbow_mount,navy,"upper",printed=True)
    add("elbow-mount-bridge",box(36,16,2,(60.2,-14.5,-7.5)),navy,"upper",printed=True)
    add("S3-elbow-SG90",servo_shape,blue,"elbow_case",tr(0,-12,0)@rot("x",-90))
    # White horns rotate with the driven links. All splines remain supplier parts.
    horn=box(21,5,1.8,(7,0,0)).fuse(cyl(4,1.8,(-1,0,-.9)))
    for group in ("upper","fore"):
        add(group+"-supplied-horn",horn,"#f2f4ed",group,tr(0,-8.7,0)@rot("x",90))
    add("S4-gripper-SG90",servo_shape,blue,"wrist",tr(5,0,0))
    gripper_frame=box(38,24,3,(10,0,-7)).cut(box(24,13,8,(10.2,0,-7)))
    add("gripper-cradle",gripper_frame,navy,"wrist",printed=True)
    # One active jaw on the servo horn and one fixed jaw: an actual single-servo gripper.
    fixed=box(35,3,5,(21,-12,4)).fuse(box(3,10,5,(37,-8.5,4)))
    moving=box(30,3,5,(15,0,0)).fuse(box(3,10,5,(29,-3.5,0)))
    add("fixed-jaw",fixed,ivory,"wrist",printed=True)
    add("moving-jaw",moving,ivory,"jaw",printed=True)
    add("gripper-horn",horn,"#f2f4ed","jaw",tr(0,0,-2))
    add("yaw-horn",horn,"#f2f4ed","yaw",tr(0,0,39))
    # Grippy pads are separate replaceable parts.
    add("fixed-pad",box(2,7,4,(35,-8,4)),orange,"wrist")
    add("moving-pad",box(2,7,4,(27,-3.5,0)),orange,"jaw")
    for item in list(parts):
        if "SG90" in item["name"]:
            add(item["name"]+"-label",box(19,.16,10,(5.2,-6.22,-16)),"#152f40",item["group"],item["local"])
            text=cq.Workplane("XZ").text("SG90",3.4,.1,font="DejaVu Sans").val().translate((5.2,-6.32,-16))
            add(item["name"]+"-label-text",text,"#eac273",item["group"],item["local"])
    return parts


def frames(yaw=0, shoulder=52, elbow=-68, jaw=25):
    z=rot("z",yaw); shoulder_case=z@tr(0,0,65)
    upper=shoulder_case@rot("y",-shoulder)
    elbow_case=upper@tr(55,0,0)
    fore=elbow_case@rot("y",-elbow)
    wrist=fore@tr(45,0,0)
    return dict(static=np.eye(4),yaw=z,shoulder_case=shoulder_case,upper=upper,
                elbow_case=elbow_case,fore=fore,wrist=wrist,jaw=wrist@tr(5,0,4)@rot("z",jaw))


def transformed(shape,m):
    from OCP.gp import gp_Trsf
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    transform=gp_Trsf(); transform.SetValues(*[float(v) for v in m[:3,:].flatten()])
    return cq.Shape.cast(BRepBuilderAPI_Transform(shape.wrapped,transform,True).Shape())


def main():
    parser=argparse.ArgumentParser(); parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--reuse-only",action="store_true"); parser.add_argument("--no-render",action="store_true")
    args=parser.parse_args(); out=args.output.resolve(); out.mkdir(parents=True,exist_ok=True)
    os.environ["DATABASE_BACKEND"]="sqlite"; os.environ["SQLITE_DATABASE_URL"]=f"sqlite:///{out}/demo.sqlite"
    os.environ["FORMA_CLI_ARTIFACT_STORAGE_BACKEND"]="local"
    os.environ["FORMA_CLI_ARTIFACT_STORAGE_DIR"]=str(out/"artifact-store")
    from forma_core.assets.models import AssetScope, ComponentIdentity, Registration
    from forma_core.assets.repository import AssetRepository
    from forma_core.assets.service import ComponentAssetLibrary
    from forma_core.persistence.project_artifacts import ProjectArtifactStorage
    from forma_core import database
    from apps.api.component_assets import call_asset_tool
    from forma_core.assets.models import AssetToolArguments
    from forma_core.opencode.capabilities import ConnectorCapability
    from forma_core.workspaces.projects.models import HardwareIntermediateRepresentation
    database.init_db()
    library=ComponentAssetLibrary(AssetRepository(database.get_database_provider()),ProjectArtifactStorage(),AssetScope(owner_user_id="sg90-demo"))
    identity=ComponentIdentity(manufacturer="TowerPro",part_number="SG90",revision="photo-reference-v1",
                               variants={"control":"positional", "case":"23x12.2x29-mm-reference", "model":"generated-envelope"})
    external={"web_searches":0,"source_downloads":0,"cad_generations":0,"preview_conversions":0}
    def source(_identity):
        if args.reuse_only: raise RuntimeError("Restart run must not generate, download, or convert a servo")
        external["cad_generations"]+=1
        model=servo(); step=out/"sg90-reference.step"; stl=out/"sg90-reference.stl"
        cq.exporters.export(model,str(step)); cq.exporters.export(model,str(stl),tolerance=.08,angularTolerance=.12)
        external["preview_conversions"]+=1
        contents={"original":step.read_bytes(),"preview":stl.read_bytes()}
        shas={k:hashlib.sha256(v).hexdigest() for k,v in contents.items()}
        reg=Registration.model_validate(dict(identity=identity.model_dump(), aliases=["SG-90"],
            provenance=dict(origin="generated",source_urls=["https://towerpro.com.tw/product/sg90-analog/"],
            product_url="https://towerpro.com.tw/product/sg90-analog/",retrieved_at=datetime.now(timezone.utc),
            license="MIT (this generated reference CAD)",usage_notes="Not vendor CAD. Reference created for this example from published nominal size and the supplied photo."),
            units="mm", dimensions_mm=[32.3,12.2,29], approximate=True,
            limitations=["Photo suggests SG90; exact manufacturer/revision is not confirmed.",
                        "Ear hole spacing 28.2 mm, horn, case details, shaft and spline are assumptions; measure actual servos before making brackets.",
                        "Housing is a simplified reference, not a validated vendor model."],
            representations=[dict(name="original",format="step",media_type="model/step",sha256=shas["original"],size_bytes=len(contents["original"])),
                dict(name="preview",format="stl",media_type="model/stl",sha256=shas["preview"],size_bytes=len(contents["preview"]),
                     derived_from=shas["original"],conversion={"engine":"CadQuery/OCCT", "linear_deflection_mm":.08,"angular_deflection_rad":.12})]))
        return reg,contents
    result=library.resolve(identity,source,allow_approximate=True)
    if result["status"]!="hit": raise RuntimeError(result)
    asset=result["asset"]; project_id="a3e67cde-4b49-4d9e-bc2a-75c7e52d4002" if args.reuse_only else "a3e67cde-4b49-4d9e-bc2a-75c7e52d4001"
    project=HardwareIntermediateRepresentation.model_validate({"overview":{"title":"SG90 four-servo arm", "description":"Compact reference robot arm; unbuilt engineering prototype.","difficulty":"Intermediate","category":"Robotics"},
        "components":[{"ref_des":f"S{i}","part_number":"SG90","name":label,"category":"Actuator", "rationale":"Pictured 9 g positional micro-servo", "pins":[]}
                      for i,label in enumerate(["Base yaw","Shoulder pitch","Elbow pitch","Gripper"],1)],"nets":[]})
    database.persist_chat_project_revision(project_id,"sg90-demo",project,source_job_id="sg90-demo-initial",prompt="Build a robot arm with four pictured SG90 servos",visibility="private")
    cap=ConnectorCapability("demo","demo",project_id,"sg90-demo",2_000_000_000,"demo",frozenset({"mcp"}))
    transforms=frames()
    from scipy.spatial.transform import Rotation
    placements=[tr(0,0,37),transforms["shoulder_case"]@tr(0,-12,0)@rot("x",-90),transforms["elbow_case"]@tr(0,-12,0)@rot("x",-90),transforms["wrist"]@tr(5,0,0)]
    for i,matrix in enumerate(placements,1):
        request=dict(asset_id=asset["asset_id"],version=asset["version"],instance_id=f"S{i}",position_mm=matrix[:3,3].tolist(),rotation_deg=Rotation.from_matrix(matrix[:3,:3]).as_euler("xyz",degrees=True).tolist(),allow_approximate=True)
        response=call_asset_tool("forma.opencode.asset_attach",AssetToolArguments(request_json=json.dumps(request)),cap,lambda: None)
        assert response["status"]=="attached",response
    latest=database.get_latest_project_revision(project_id,"sg90-demo")
    proof=dict(project_id=project_id,asset_id=asset["asset_id"],version=asset["version"],instances=4,
               external_work=external,library_metrics=library.metrics,revision_id=str(latest.revision_id),
               pinned_instances=list(latest.state.assembly_metadata["component_asset_refs"]),
               note="Generated approximate reference; this proves library reuse and persistence, not #530 web sourcing or physical performance.")
    proof_path=out/("reuse-after-restart.json" if args.reuse_only else "first-ingestion.json")
    if args.reuse_only or not proof_path.exists(): proof_path.write_text(json.dumps(proof,indent=2))
    (out/("second-project.json" if args.reuse_only else "first-project.json")).write_text(latest.model_dump_json(indent=2))
    if args.reuse_only:
        print(json.dumps(proof)); return
    (out/"asset-metadata.json").write_text(json.dumps(asset,indent=2))
    # CAD is built from the stored bytes returned by the same library, not regenerated per instance.
    cached=out/"reused-servo.step"; cached.write_bytes(library.content(asset["asset_id"],asset["version"],"original"))
    (out/"reused-servo.stl").write_bytes(library.content(asset["asset_id"],asset["version"],"preview"))
    imported=cq.importers.importStep(str(cached)).val()
    parts=make_parts(imported); pose=frames()
    assembly=cq.Assembly(name="SG90 compact robot arm")
    printed=out/"printed-parts"; printed.mkdir(exist_ok=True)
    masses={}
    for item in parts:
        shape=transformed(item["shape"],pose[item["group"]]@item["local"])
        assembly.add(shape,name=item["name"],color=cq.Color(item["color"]))
        if item["printed"]:
            cq.exporters.export(item["shape"],str(printed/(item["name"]+".stl")),tolerance=.08)
            masses[item["name"]]=round(item["shape"].Volume()*.00124,2) # solid PLA estimate in grams
    assembly.save(str(out/"sg90-robot-arm.step"))
    (out/"printed-mass-estimate.json").write_text(json.dumps(masses,indent=2))
    database.get_database_provider().dispose()  # Release WAL handles before the restart proof.
    subprocess.run([sys.executable,str(Path(__file__).resolve()),"--output",str(out),"--reuse-only"],check=True)
    if not args.no_render: render(parts,out)
    print(json.dumps(proof))


def render(parts,out):
    import pyvista as pv
    from PIL import Image, ImageDraw, ImageFont
    import imageio.v2 as imageio
    p=pv.Plotter(off_screen=True,window_size=(1100,820))
    p.set_background("#e9eff4"); actors=[]
    servo_mesh=pv.read(out/"reused-servo.stl")
    for item in parts:
        if item["name"].endswith("SG90"):
            mesh=servo_mesh  # Reuse the registered derived mesh for all four instances.
        else:
            vertices,triangles=item["shape"].tessellate(.12,.15)
            mesh=pv.PolyData(np.array([[v.x,v.y,v.z] for v in vertices]),np.array([[3,*t] for t in triangles]).flatten())
        actor=p.add_mesh(mesh,color=item["color"],smooth_shading=True,specular=.4,specular_power=30,
                         opacity=1)
        actors.append((actor,item))
    floor=pv.Plane(center=(20,0,-.5),direction=(0,0,1),i_size=500,j_size=500)
    p.add_mesh(floor,color="#dce5ed",smooth_shading=True)
    p.camera_position=[(240,-330,225),(30,0,66),(0,0,1)]
    p.camera.parallel_projection=True; p.camera.parallel_scale=116
    p.add_light(pv.Light(position=(100,-200,350),focal_point=(25,0,60),intensity=.7))
    p.enable_anti_aliasing("ssaa")
    try:
        font=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",17)
        title=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",32)
        small=ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",14)
    except OSError: font=title=small=ImageFont.load_default()
    images=[]
    for n in range(96):
        t=n/96; wave=.5-.5*math.cos(2*math.pi*t)
        f=frames(yaw=-25+50*wave,shoulder=40+30*wave,elbow=-55-20*wave,jaw=8+35*(.5+.5*math.cos(4*math.pi*t)))
        for actor,item in actors: actor.user_matrix=f[item["group"]]@item["local"]
        p.render()
        im=Image.fromarray(p.screenshot(return_img=True)); draw=ImageDraw.Draw(im)
        draw.rectangle((0,0,1100,108),fill="#112536")
        draw.text((38,20),"SG90 / COMPACT ROBOT ARM",font=title,fill="#f4f8fc")
        draw.text((40,67),"4 micro servos  ·  55 + 45 mm links  ·  one reusable component asset",font=font,fill="#a9d2eb")
        draw.rounded_rectangle((35,700,1065,794),radius=14,fill="#ffffff")
        draw.text((54,716),"BASE YAW    /    SHOULDER    /    ELBOW    /    SINGLE-SERVO GRIPPER",font=font,fill="#173348")
        draw.text((54,748),"Kinematic CAD preview · Approximate SG90 reference · Mounting fit and load capacity need bench testing",font=small,fill="#486276")
        images.append(im)
        if n==20: im.save(out/"sg90-robot-arm.png")
    images[0].save(out/"sg90-robot-arm.gif",save_all=True,append_images=images[1:],duration=65,loop=0,optimize=False)
    p.close()


if __name__=="__main__": main()
