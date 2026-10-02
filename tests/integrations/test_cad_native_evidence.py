"""Contract tests with synthetic native measurements, not licensed CAD certification."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

from forma_core.cad_migrations import MigrationModel, plan_migration
from forma_core.cad_migrations.cli import _json
from forma_core.cad_migrations.evidence import GeometryMetrics, NativeEvidence, compare_evidence
from forma_core.cad_migrations.form_history import from_project
from forma_core.cad_migrations.inventor import extract_document
from forma_core.cad_migrations.emitters import python_rebuild
from forma_core.workspaces.projects.solid_cad import CadOperation

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/cad-migrations/bracket.solidworks.json"


def sample():
    value = json.loads(EXAMPLE.read_text())
    value["source"]["system"] = "inventor"
    value["source_geometry"] = {"body_count": 1, "volume_mm3": 3000., "minimum_mm": [0., 0., 0.], "maximum_mm": [40., 20., 4.]}
    return MigrationModel.model_validate(value)


def evidence(model):
    return NativeEvidence(target="fusion360", application_version="synthetic-test-only",
        source_sha256=model.source.sha256, rebuild_sha256=hashlib.sha256(_json(model.normalized())).hexdigest(),
        status="rebuilt_unverified", geometry=model.source_geometry,
        feature_ids=[f.id for f in model.features], parameter_checks={p: True for p in model.parameters},
        metadata=model.metadata.model_dump())


class EvidenceTests(unittest.TestCase):
    def test_report_checks_never_claim_shape_equivalence(self):
        model = sample()
        result = compare_evidence(model, "fusion360", evidence(model))
        self.assertEqual(result["status"], "checks_passed")
        self.assertFalse(result["geometry_equivalence_verified"])
        self.assertEqual(result["evidence_origin"], "user_uploaded_native_report")

    def test_reports_are_bound_to_exact_source_history_and_target(self):
        model = sample()
        for field, value in (("source_sha256", "1"*64), ("rebuild_sha256", "1"*64), ("target", "nx")):
            with self.subTest(field=field), self.assertRaises(ValueError):
                compare_evidence(model, "fusion360", evidence(model).model_copy(update={field: value}))

    def test_missing_or_failed_checks_cannot_pass(self):
        model = sample()
        for update in ({"status": "failed"}, {"error": "Native error"}, {"feature_ids": ["base"]},
                       {"parameter_checks": {}}, {"parameter_checks": {"unknown": True}}, {"metadata": {}},
                       {"geometry": model.source_geometry.model_copy(update={"volume_mm3": 3050.})},
                       {"geometry": model.source_geometry.model_copy(update={"body_count": 2})},
                       {"geometry": model.source_geometry.model_copy(update={"minimum_mm": [-1., 0., 0.]})}):
            with self.subTest(update=update):
                self.assertEqual(compare_evidence(model, "fusion360", evidence(model).model_copy(update=update))["status"], "needs_repair")

    def test_no_baseline_or_no_parameters_requires_review(self):
        model = sample(); report = evidence(model)
        model.source_geometry = None
        report.rebuild_sha256 = hashlib.sha256(_json(model.normalized())).hexdigest()
        self.assertEqual(compare_evidence(model, "fusion360", report)["status"], "needs_repair")

    def test_metrics_reject_nonfinite_inverted_and_degenerate_bounds(self):
        for change in ({"volume_mm3": float("nan")}, {"maximum_mm": [0., 1., 1.]}, {"body_count": True}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                GeometryMetrics.model_validate({**sample().source_geometry.model_dump(), **change})

    def test_obviously_disjoint_cuts_and_joins_block(self):
        model = sample()
        model.features[1].origin = (5000., 5000., 0.)
        self.assertIn("cut_outside_body_bounds", [b["code"] for b in plan_migration(model, "fusion360")["blockers"]])
        model.features[1].operation = "join"
        self.assertIn("disconnected_join", [b["code"] for b in plan_migration(model, "fusion360")["blockers"]])

    def test_multibody_source_blocks_and_tangent_cut_blocks(self):
        model = sample(); model.source_geometry.body_count = 2
        self.assertEqual(plan_migration(model, "fusion360")["blockers"][0]["code"], "single_solid_source_required")
        model = sample(); model.features[1].origin = (43., 10., 0.)
        self.assertIn("cut_outside_body_bounds", [b["code"] for b in plan_migration(model, "fusion360")["blockers"]])

    def test_form_bridge_preserves_centered_geometry_and_blocks_engravings(self):
        op = CadOperation(shape="box", size={"x_mm": 40, "y_mm": 20, "z_mm": 4})
        state = NS(mechanical=NS(cad_operations=[op]))
        model = from_project(state)
        self.assertEqual(model.features[0].origin, (-20., -10., -2.))
        self.assertEqual(model.resolve(model.features[0].width), 40.)
        self.assertEqual(plan_migration(model, "fusion360")["status"], "ready_for_rebuild")
        op.axis_labels = True
        self.assertEqual(plan_migration(from_project(state), "fusion360")["status"], "blocked")
        with self.assertRaises(ValueError): from_project(NS(mechanical=None))


class Collection:
    def __init__(self, *items): self.items = items; self.Count = len(items)
    def Item(self, index): return self.items[index-1]


class InventorBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(TemporaryDirectory())
        path = Path(self.temp) / "part.ipt"; path.write_bytes(b"synthetic native file")
        self.c = NS(kPartDocumentObject=1, kExtrudeFeatureObject=2, kSketchCircleObject=3, kSketchLineObject=4,
                    kDistanceExtent=5, kPositiveExtentDirection=6, kNewBodyOperation=7, kJoinOperation=8, kCutOperation=9)
        circle = NS(Type=3, Geometry=NS(Center=NS(X=1., Y=1.), Radius=.3))
        profile_path = Collection(NS(SketchEntity=circle)); profile_path.TextBoxPath = False; profile_path.Closed = True; profile_path.AddsMaterial = True
        profile = Collection(profile_path)
        profile.Parent = NS(PlanarEntityGeometry=NS(Normal=NS(X=0., Y=0., Z=1.)), DimensionConstraints=Collection(),
            SketchToModelSpace=lambda point: NS(X=point.X, Y=point.Y, Z=0.))
        self.definition = NS(IsTwoDirectional=False, ExtentType=5, Extent=NS(Direction=6, Distance=NS(Expression="4 mm", Value=.4)),
                             TaperAngle=NS(Expression="0 deg", Value=0.), Profile=profile, Operation=7)
        self.feature = NS(Name="Cylinder", Type=2, Suppressed=False, Definition=self.definition)
        body = NS(IsSolid=True, RangeBox=NS(MinPoint=NS(X=.7, Y=.7, Z=0.), MaxPoint=NS(X=1.3, Y=1.3, Z=.4)))
        self.document = NS(FullFileName=str(path), DocumentType=1, Dirty=False,
            ComponentDefinition=NS(Features=Collection(self.feature), SurfaceBodies=Collection(body), MassProperties=NS(Volume=.1130973355)),
            PropertySets=NS(Item=lambda guid: Collection(NS(Name="Finish code", Value="Anodized")) if guid.startswith("{D5CDD505") else NS(ItemByPropId=lambda id: NS(Value=f"property-{id}"))))

    def test_reads_saved_identity_mm_geometry_and_complete_inventory(self):
        model = extract_document(self.document, self.c, "test-only")
        self.assertTrue(model.inventory_complete)
        self.assertEqual(model.source.sha256, hashlib.sha256(b"synthetic native file").hexdigest())
        self.assertEqual(model.features[0].origin, (10., 10., 0.))
        self.assertEqual(model.parameters, {"F1_radius": 3., "F1_depth": 4.})
        self.assertAlmostEqual(model.source_geometry.volume_mm3, 113.0973355)
        self.assertEqual(json.loads(next(iter(model.metadata.properties.values()))), {"name": "Finish code", "value": "Anodized"})
        self.assertEqual(plan_migration(model, "fusion360")["status"], "ready_for_rebuild")

    def test_unsupported_features_are_retained_not_dropped(self):
        self.document.ComponentDefinition.Features = Collection(self.feature, NS(Name="Fillet", Type=99, Suppressed=False))
        model = extract_document(self.document, self.c, "test-only")
        self.assertEqual(len(model.features), 2)
        self.assertEqual(model.features[1].kind, "unsupported")
        self.assertEqual(plan_migration(model, "fusion360")["status"], "blocked")

    def test_linked_dimensions_taper_and_reversed_extents_fail_closed(self):
        for changed in ("equation", "taper", "reverse"):
            document = deepcopy(self.document)
            definition = document.ComponentDefinition.Features.Item(1).Definition
            if changed == "equation": definition.Extent.Distance.Expression = "width / 2"
            if changed == "taper": definition.TaperAngle.Value = .1
            if changed == "reverse": definition.Extent.Direction = 99
            self.assertEqual(extract_document(document, self.c, "test-only").features[0].kind, "unsupported")

    def test_unsaved_or_dirty_document_is_rejected(self):
        self.document.Dirty = True
        with self.assertRaises(ValueError): extract_document(self.document, self.c, "test-only")

    def test_credential_like_custom_properties_are_rejected(self):
        original = self.document.PropertySets.Item
        self.document.PropertySets.Item = lambda guid: Collection(NS(Name="API Token", Value="not-a-real-credential")) if guid.startswith("{D5CDD505") else original(guid)
        with self.assertRaisesRegex(ValueError, "Credential-like"):
            extract_document(self.document, self.c, "test-only")


class FusionEvidenceProgramTests(unittest.TestCase):
    def run_program(self, *, changes_shape=True, export_succeeds=True):
        root = Path(self.enterContext(TemporaryDirectory()))
        model = sample().normalized()
        parameters = {name: NS(expression=f"{value} mm") for name, value in model["parameters"].items()}
        baseline = {name: item.expression for name, item in parameters.items()}
        class Body:
            meshManager = NS(createMeshCalculator=lambda: NS(calculate=lambda: NS(nodeCoordinates=[NS(x=0., y=0., z=0.), NS(x=4., y=0., z=0.), NS(x=0., y=2., z=0.)], nodeIndices=[0, 1, 2])))
            isSolid = True
            boundingBox = NS(minPoint=NS(x=0., y=0., z=0.), maxPoint=NS(x=4., y=2., z=.4))
            @property
            def volume(self):
                return 3. + (sum(float(p.expression.split()[0]) for p in parameters.values()) / 1000 if changes_shape else 0.)
        def attrs(source_id): return NS(itemByName=lambda group, name: NS(value=source_id))
        features = [NS(healthState=0, attributes=attrs(feature["id"])) for feature in model["features"]]
        component = NS(bRepBodies=NS(count=1, item=lambda i: Body()),
            features=NS(count=len(features), item=lambda i: features[i]),
            partNumber=model["metadata"]["part_number"], description=model["metadata"]["description"],
            attributes=NS(itemByName=lambda *a: NS(value=json.dumps({"metadata": model["metadata"]}))))
        def export(path):
            if export_succeeds: Path(path).write_bytes(b"synthetic native output")
            return export_succeeds
        design = NS(userParameters=NS(itemByName=lambda name: parameters[name.removeprefix("forma_")]), computeAll=lambda: True,
            exportManager=NS(createSTEPExportOptions=lambda path, component: path, createFusionArchiveExportOptions=lambda path, component: path, execute=export))
        fusion = NS(FeatureHealthStates=NS(HealthyFeatureHealthState=0))
        program = {}; exec(compile(python_rebuild("fusion360", "1"*64), "generated-rebuild.py", "exec"), program)
        with patch.dict("sys.modules", {"adsk": NS(fusion=fusion), "adsk.fusion": fusion}):
            if not export_succeeds:
                with self.assertRaisesRegex(RuntimeError, "Native export failed"):
                    program["fusion_evidence"](root, model, design, component, NS(version="test-only"))
                self.assertEqual(list(root.glob("evidence-*.json")), [])
                return
            program["fusion_evidence"](root, model, design, component, NS(version="test-only"))
        self.assertEqual({name: item.expression for name, item in parameters.items()}, baseline)
        report = NativeEvidence.model_validate_json(next(root.glob("evidence-*.json")).read_bytes())
        self.assertEqual(report.feature_ids, [f["id"] for f in model["features"]])
        self.assertEqual(set(report.parameter_checks.values()), {changes_shape})
        self.assertEqual(len(report.native_artifacts), 3)
        self.assertIn("target.mesh.json", report.native_artifacts)
        for name, digest in report.native_artifacts.items():
            self.assertEqual(hashlib.sha256((root/name).read_bytes()).hexdigest(), digest)

    def test_generated_program_measures_edits_restores_and_exports(self): self.run_program()
    def test_non_regenerating_parameters_are_not_reported_as_passing(self): self.run_program(changes_shape=False)
    def test_failed_export_does_not_write_success_evidence(self): self.run_program(export_succeeds=False)


if __name__ == "__main__": unittest.main()
