from __future__ import annotations

import unittest
from types import SimpleNamespace

from forma_core.agents.pipeline import GenerationStageRun, GenerationStageSpec
from forma_core.agents.orchestrator import (
    DefaultAssemblyOutput,
    DefaultComponentSelection,
    DefaultValidationOutput,
    DefaultWiringOutput,
    HardwarePipelineOrchestrator,
)
from forma_core.agents.web_research_workflow import (
    AssemblyWrapper,
    CompletenessAudit,
    WebComponentSelection,
    WebProjectPlan,
    WebResearchHardwarePipeline,
    WiringWrapper,
)
from forma_core.external_sources import ExternalSourceLibrary
from forma_core.workspaces.projects.models import (
    ComponentInstance,
    ConnectionNet,
    FunctionalRequirements,
    MechanicalNotes,
    PinDefinition,
    PinReference,
    ProjectOverview,
    SystemArchitecture,
    SystemNode,
)


def project_plan() -> WebProjectPlan:
    return WebProjectPlan(
        overview=ProjectOverview(
            title="Partial robot",
            description="A small independently driven robot.",
            difficulty="Intermediate",
            category="Robotics",
        ),
        requirements=FunctionalRequirements(
            requirements=["Drive four wheels"],
            power_needs="Low-voltage battery",
        ),
        system_architecture=SystemArchitecture(
            summary="Robot system",
            root=SystemNode(
                system_id="product",
                name="Robot",
                domain="product",
                purpose="Move on four wheels.",
            ),
        ),
    )


def component_selection() -> WebComponentSelection:
    return WebComponentSelection(components=[
        ComponentInstance(
            ref_des="U1",
            part_number="MCU-TEST",
            name="Test controller",
            category="Microcontroller",
            unit_price=4.0,
            rationale="Controls the robot.",
            pins=[
                PinDefinition(pin_id="VCC", name="Power", pin_type="Power", voltage=5),
                PinDefinition(pin_id="GND", name="Ground", pin_type="Ground"),
            ],
        ),
        ComponentInstance(
            ref_des="J1", name="Power input", category="Connector", rationale="Supplies the controller",
            pins=[
                PinDefinition(pin_id="VCC", name="Power", pin_type="Power", voltage=5),
                PinDefinition(pin_id="GND", name="Ground", pin_type="Ground"),
            ],
        )
    ])


def mechanical_plan() -> MechanicalNotes:
    return MechanicalNotes(
        enclosure_type="Open frame",
        mounting_guidance="Mount the controller above the chassis.",
        manufacturability_rating="Easy",
        fabrication_details=["Use a flat chassis plate."],
    )


class GenerationStageRunTests(unittest.TestCase):
    def test_component_stage_snapshots_preserve_electrical_details(self) -> None:
        for schema in (DefaultComponentSelection, WebComponentSelection):
            with self.subTest(schema=schema.__name__):
                selection = schema(components=component_selection().components)
                restored = schema.model_validate_json(selection.model_dump_json())
                self.assertEqual(selection.components, restored.components)
                self.assertEqual("Microcontroller", restored.components[0].category)
                self.assertEqual(2, len(restored.components[0].pins))

    def test_stage_failure_blocks_dependents_but_runs_independent_work(self) -> None:
        checkpoints: list[dict] = []
        run = GenerationStageRun(
            "web_research",
            [
                GenerationStageSpec(stage_id="components"),
                GenerationStageSpec(stage_id="wiring", dependencies=["components"]),
                GenerationStageSpec(stage_id="validation", dependencies=["wiring"]),
                GenerationStageSpec(stage_id="mechanical", dependencies=["components"]),
            ],
            persist=lambda current, _record: checkpoints.append(current.snapshot()),
        )

        run.run("components", lambda: {"refs": ["U1"]})
        run.run("wiring", lambda: (_ for _ in ()).throw(TimeoutError("wiring timed out")))
        run.run("validation", lambda: {"valid": True})
        run.run("mechanical", lambda: {"enclosure": "open-frame"})

        self.assertEqual("succeeded", run.records["components"].status.value)
        self.assertEqual("failed", run.records["wiring"].status.value)
        self.assertEqual("blocked", run.records["validation"].status.value)
        self.assertEqual("succeeded", run.records["mechanical"].status.value)
        self.assertEqual("partial", run.overall_status)
        self.assertEqual("generation-stage:components", run.records["components"].artifact.kind)
        self.assertTrue(run.records["components"].artifact.checksum.startswith("sha256:"))
        self.assertTrue(any(
            snapshot["records"]["components"]["status"] == "succeeded"
            for snapshot in checkpoints
        ))


class WebResearchPartialGenerationTests(unittest.TestCase):
    def pipeline(self) -> tuple[WebResearchHardwarePipeline, list[dict]]:
        pipeline = WebResearchHardwarePipeline.__new__(WebResearchHardwarePipeline)
        pipeline.runtime_config = SimpleNamespace(
            provider="test",
            model="test-model",
            requested_provider="test",
            requested_model="test-model",
            provider_overridden=False,
            model_overridden=False,
        )
        pipeline.model_name = "test-model"
        pipeline.research_client = SimpleNamespace(provider_name="test-search")
        pipeline._active_generation_metadata = {
            "project_id": "11111111-1111-4111-8111-111111111111",
            "frontend_job_id": "job-stage-test",
            "owner_user_id": "stage-owner",
            "project_prompt": "Build a four wheel robot",
        }
        saved: list[dict] = []
        pipeline._save_project_to_db = lambda prompt, ir: (
            saved.append(ir.model_dump(mode="json"))
            or "11111111-1111-4111-8111-111111111111"
        )
        pipeline._research = lambda queries: ExternalSourceLibrary(
            provider="test-search",
            configured=True,
            searches_attempted=len(queries),
        )
        pipeline._plan_project = lambda *args: project_plan()
        pipeline._select_components = lambda *args: component_selection()
        pipeline._generate_mechanical = lambda *args: mechanical_plan()
        pipeline._generate_assembly = lambda *args: []
        pipeline._audit_output = lambda *args: CompletenessAudit(
            completeness_score=1.0,
            summary="Complete",
        )
        return pipeline, saved

    @staticmethod
    def model_validation() -> SimpleNamespace:
        return SimpleNamespace(
            fallback_active=False,
            requested_model="test-model",
            actual_model="test-model",
            provider="test",
        )

    def test_wiring_failure_preserves_bom_and_mechanical_artifacts(self) -> None:
        pipeline, saved = self.pipeline()
        pipeline._wire_project = lambda *args: (_ for _ in ()).throw(TimeoutError("wiring timed out"))

        ir = pipeline._generate_staged_project(
            "Build a four wheel robot",
            image_bytes=None,
            image_mime_type=None,
            model_validation=self.model_validation(),
        )

        records = ir.assembly_metadata["generation_run"]["records"]
        self.assertEqual("partial", ir.assembly_metadata["generation_status"])
        self.assertEqual("partial", ir.assembly_metadata["project_readiness"])
        self.assertEqual("succeeded", records["web_architect"]["status"])
        self.assertEqual("succeeded", records["web_component_sourcing"]["status"])
        self.assertEqual("failed", records["wiring_netlist"]["status"])
        self.assertEqual("blocked", records["validation_repair"]["status"])
        self.assertEqual("succeeded", records["mechanical_fabrication"]["status"])
        self.assertEqual("blocked", records["assembly"]["status"])
        self.assertEqual(2, len(ir.bom))
        self.assertIsNotNone(ir.mechanical)
        self.assertGreater(len(saved), 4)
        component_checkpoint = next(
            item for item in saved
            if ((item.get("assembly_metadata") or {}).get("generation_run") or {})
            .get("records", {})
            .get("web_component_sourcing", {})
            .get("status") == "succeeded"
        )
        self.assertEqual(2, len(component_checkpoint["bom"]))

    def test_retry_reuses_upstream_and_independent_artifacts(self) -> None:
        pipeline, _ = self.pipeline()
        pipeline._wire_project = lambda *args: (_ for _ in ()).throw(TimeoutError("first attempt"))
        partial = pipeline._generate_staged_project(
            "Build a four wheel robot",
            image_bytes=None,
            image_mime_type=None,
            model_validation=self.model_validation(),
        )

        prior_run = partial.assembly_metadata["generation_run"]
        pipeline._active_generation_metadata.update({
            "retry_stage": "wiring_netlist",
            "prior_generation_run": prior_run,
        })
        pipeline._research = lambda *_: (_ for _ in ()).throw(AssertionError("research repeated"))
        pipeline._plan_project = lambda *_: (_ for _ in ()).throw(AssertionError("architecture repeated"))
        pipeline._select_components = lambda *_: (_ for _ in ()).throw(AssertionError("components repeated"))
        pipeline._generate_mechanical = lambda *_: (_ for _ in ()).throw(AssertionError("mechanical repeated"))
        pipeline._wire_project = lambda *args: WiringWrapper(nets=[
            ConnectionNet(net_id=pin_id, name=pin_id, net_type=net_type, pins=[
                PinReference(ref_des=ref, pin_id=pin_id) for ref in ("U1", "J1")
            ]) for pin_id, net_type in (("VCC", "Power"), ("GND", "Ground"))
        ], pin_mappings=[])

        completed = pipeline._generate_staged_project(
            "Build a four wheel robot",
            image_bytes=None,
            image_mime_type=None,
            model_validation=self.model_validation(),
        )

        records = completed.assembly_metadata["generation_run"]["records"]
        self.assertEqual("succeeded", completed.assembly_metadata["generation_status"], {
            key: record.get("error") for key, record in records.items() if record["status"] == "failed"
        })
        self.assertEqual("complete", completed.assembly_metadata["project_readiness"])
        self.assertEqual(1, records["web_architect"]["attempt"])
        self.assertEqual(1, records["web_component_sourcing"]["attempt"])
        self.assertEqual(1, records["mechanical_fabrication"]["attempt"])
        self.assertEqual(2, records["wiring_netlist"]["attempt"])
        self.assertEqual(2, records["package_project"]["attempt"])
        self.assertEqual(
            ["failed", "succeeded"],
            [entry["status"] for entry in records["wiring_netlist"]["attempt_history"]],
        )

        pipeline._active_generation_metadata.update({
            "prior_generation_run": completed.assembly_metadata["generation_run"],
            "retry_stage_replay": True,
        })
        pipeline._wire_project = lambda *_: (_ for _ in ()).throw(AssertionError("wiring repeated"))
        replayed = pipeline._generate_staged_project(
            "Build a four wheel robot",
            image_bytes=None,
            image_mime_type=None,
            model_validation=self.model_validation(),
        )

        replayed_records = replayed.assembly_metadata["generation_run"]["records"]
        self.assertEqual(2, replayed_records["wiring_netlist"]["attempt"])
        self.assertEqual("succeeded", replayed.assembly_metadata["generation_status"])


class DefaultPartialGenerationTests(unittest.TestCase):
    def pipeline(self) -> HardwarePipelineOrchestrator:
        pipeline = HardwarePipelineOrchestrator.__new__(HardwarePipelineOrchestrator)
        pipeline.runtime_config = SimpleNamespace(
            provider="test",
            model="test-model",
            requested_provider="test",
            requested_model="test-model",
        )
        pipeline.model_name = "test-model"
        pipeline.persist_project = False
        pipeline._active_generation_metadata = {
            "project_id": "22222222-2222-4222-8222-222222222222",
            "frontend_job_id": "default-stage-test",
            "project_prompt": "Build a four wheel robot",
        }
        pipeline._generate_default_overview = lambda *args: project_plan().overview
        pipeline._generate_default_requirements = lambda *args: project_plan().requirements
        pipeline._generate_default_architecture = lambda *args: project_plan().system_architecture
        pipeline._generate_default_components = lambda *args: DefaultComponentSelection(
            components=component_selection().components,
        )
        pipeline._generate_default_mechanical = lambda *args: mechanical_plan()
        pipeline._generate_default_assembly = lambda *args: DefaultAssemblyOutput(steps=[])
        return pipeline

    @staticmethod
    def model_validation() -> SimpleNamespace:
        return SimpleNamespace(
            fallback_active=False,
            requested_model="test-model",
            actual_model="test-model",
            provider="test",
        )

    def test_default_wiring_failure_is_partial_and_retry_reuses_completed_stages(self) -> None:
        checkpoints: list[dict] = []
        pipeline = self.pipeline()
        pipeline._active_generation_metadata["stage_checkpoint"] = checkpoints.append
        pipeline._generate_default_wiring = lambda *args: (_ for _ in ()).throw(TimeoutError("wiring timed out"))

        partial = pipeline._generate_staged_project(
            "Build a four wheel robot",
            image_bytes=None,
            image_mime_type=None,
            model_validation=self.model_validation(),
        )

        records = partial.assembly_metadata["generation_run"]["records"]
        self.assertEqual("partial", partial.assembly_metadata["generation_status"])
        self.assertEqual("succeeded", records["component_selection"]["status"])
        self.assertEqual("failed", records["wiring_netlist"]["status"])
        self.assertEqual("blocked", records["validation_repair"]["status"])
        self.assertEqual("succeeded", records["bom"]["status"])
        self.assertEqual("succeeded", records["mechanical_fabrication"]["status"])
        self.assertEqual("blocked", records["assembly"]["status"])
        self.assertEqual(2, len(partial.bom))
        self.assertTrue(any(
            checkpoint.get("record", {}).get("stage_id") == "component_selection"
            and checkpoint["record"]["status"] == "succeeded"
            for checkpoint in checkpoints
            if isinstance(checkpoint.get("record"), dict)
        ))

        pipeline._active_generation_metadata.update({
            "prior_generation_run": partial.assembly_metadata["generation_run"],
            "retry_stage": "wiring_netlist",
        })
        pipeline._generate_default_overview = lambda *args: (_ for _ in ()).throw(AssertionError("overview repeated"))
        pipeline._generate_default_requirements = lambda *args: (_ for _ in ()).throw(AssertionError("requirements repeated"))
        pipeline._generate_default_architecture = lambda *args: (_ for _ in ()).throw(AssertionError("architecture repeated"))
        pipeline._generate_default_components = lambda *args: (_ for _ in ()).throw(AssertionError("components repeated"))
        pipeline._generate_default_mechanical = lambda *args: (_ for _ in ()).throw(AssertionError("mechanical repeated"))
        pipeline._generate_default_wiring = lambda *args: DefaultWiringOutput(nets=[], pin_mappings=[])
        pipeline._generate_default_validation = lambda *args: DefaultValidationOutput(
            nets=[],
            pin_mappings=[],
            issues=[],
            is_valid=True,
        )

        complete = pipeline._generate_staged_project(
            "Build a four wheel robot",
            image_bytes=None,
            image_mime_type=None,
            model_validation=self.model_validation(),
        )

        retried = complete.assembly_metadata["generation_run"]["records"]
        self.assertEqual("succeeded", complete.assembly_metadata["generation_status"])
        self.assertEqual("complete", complete.assembly_metadata["project_readiness"])
        self.assertEqual(1, retried["component_selection"]["attempt"])
        self.assertEqual(1, retried["mechanical_fabrication"]["attempt"])
        self.assertEqual(2, retried["wiring_netlist"]["attempt"])

    def test_default_architecture_failure_preserves_intent_and_requirements(self) -> None:
        pipeline = self.pipeline()
        pipeline._generate_default_architecture = lambda *args: (
            _ for _ in ()
        ).throw(TimeoutError("architecture timed out"))

        failed = pipeline._generate_staged_project(
            "Build a four wheel robot",
            image_bytes=None,
            image_mime_type=None,
            model_validation=self.model_validation(),
        )

        records = failed.assembly_metadata["generation_run"]["records"]
        self.assertEqual("failed", failed.assembly_metadata["generation_status"])
        self.assertEqual("succeeded", records["intent_parser"]["status"])
        self.assertEqual("succeeded", records["requirements"]["status"])
        self.assertEqual("failed", records["system_architecture"]["status"])
        self.assertEqual("blocked", records["component_selection"]["status"])
        self.assertEqual("architecture timed out", failed.assembly_metadata["generation_error"]["message"])


if __name__ == "__main__":
    unittest.main()
