"""Inspect a real STEP in an isolated native runtime, like the CAD adapter."""
import json
import os
import sys


def main():
    from cadquery import importers
    shape = importers.importStep(sys.argv[1]).val()
    bounds = shape.BoundingBox()
    print(json.dumps({
        "bounds": [bounds.xlen, bounds.ylen, bounds.zlen],
        "valid": shape.isValid(), "solids": len(shape.Solids()),
        "faces": len(shape.Faces()), "volume": shape.Volume(),
    }))


if __name__ == "__main__":
    code = 0
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    # Match cad.py: OCCT native globals can fail during Windows interpreter
    # teardown. Import/inspection failures above still exit nonzero; the caller
    # checks the result and all geometry assertions outside this process.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
