"""STEP inspection uses the OCCT kernel already used by Forma/OpenCAD."""
import tempfile
from pathlib import Path


def inspect_step(content: bytes, dimensions, tolerance: float) -> dict:
    try:
        from OCP.BRepBndLib import BRepBndLib
        from OCP.BRepCheck import BRepCheck_Analyzer
        from OCP.Bnd import Bnd_Box
        from OCP.IFSelect import IFSelect_RetDone
        from OCP.STEPControl import STEPControl_Reader
    except ImportError:
        return {"status": "unverified", "limitations": ["Install the Forma OpenCAD OCCT runtime and explicitly revalidate."]}
    with tempfile.TemporaryDirectory(prefix="forma-asset-") as folder:
        path = Path(folder) / "original.step"
        path.write_bytes(content)
        reader = STEPControl_Reader()
        if reader.ReadFile(str(path)) != IFSelect_RetDone:
            raise ValueError("The original STEP cannot be imported by OCCT.")
        reader.SetSystemLengthUnit(1.0)  # STEP units are converted to millimetres.
        if reader.TransferRoots() == 0:
            raise ValueError("The original STEP cannot be imported by OCCT.")
        shape = reader.OneShape()
        if shape.IsNull() or not BRepCheck_Analyzer(shape).IsValid():
            raise ValueError("The original STEP contains invalid geometry.")
        bounds = Bnd_Box()
        BRepBndLib.Add_s(shape, bounds)
        b = bounds.Get()
        measured = [b[i+3] - b[i] for i in range(3)]
    if dimensions and any(abs(a-b) > tolerance for a, b in zip(measured, dimensions)):
        raise ValueError("Imported STEP dimensions do not match the declared mm envelope.")
    return {"status": "validated" if dimensions else "unverified", "engine": "OCCT STEPControl",
            "dimensions_mm": measured, "units": "mm",
            "limitations": ["Geometry import and envelope checks only; mounting details and manufacturer identity need independent evidence."]}
