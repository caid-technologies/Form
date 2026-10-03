from __future__ import annotations

import unittest

from forma_core.agents.pipeline import (
    GenerationStageRun,
    GenerationStageSpec,
    GenerationStageStatus,
    list_agent_pipeline_steps,
)
from forma_core.workspaces.projects.design_lifecycle import FidelityLevel


class CostAwareStageRunnerTests(unittest.TestCase):
    def test_specs_receive_default_cost_and_fidelity(self) -> None:
        spec = GenerationStageSpec(stage_id="cad_generation")
        self.assertEqual(FidelityLevel.ASSEMBLY_CAD, spec.fidelity)
        self.assertIsNotNone(spec.cost)
        self.assertEqual("very_high", spec.cost.compute.value)

    def test_stage_artifact_records_input_and_output_fingerprints(self) -> None:
        run = GenerationStageRun(
            "default",
            [
                GenerationStageSpec(stage_id="system_architecture"),
                GenerationStageSpec(
                    stage_id="mechanical_fabrication",
                    dependencies=["system_architecture"],
                ),
            ],
        )
        run.run("system_architecture", lambda: {"root": "product"})
        run.run("mechanical_fabrication", lambda: {"form": "open-frame"})

        record = run.records["mechanical_fabrication"]
        self.assertTrue(record.input_fingerprint.startswith("sha256:"))
        self.assertTrue(record.output_fingerprint.startswith("sha256:"))
        self.assertEqual(record.output_fingerprint, record.artifact.checksum)
        self.assertEqual("geometry", record.artifact.metadata["fidelity"])
        self.assertIn("cost", record.artifact.metadata)

    def test_manual_invalidation_only_clears_transitive_dependents(self) -> None:
        run = GenerationStageRun(
            "default",
            [
                GenerationStageSpec(stage_id="system_architecture"),
                GenerationStageSpec(stage_id="component_selection", dependencies=["system_architecture"]),
                GenerationStageSpec(stage_id="mechanical_fabrication", dependencies=["system_architecture"]),
                GenerationStageSpec(stage_id="cad_generation", dependencies=["mechanical_fabrication"]),
                GenerationStageSpec(stage_id="package_project"),
            ],
        )
        run.run("system_architecture", lambda: {"tree": 1})
        run.run("component_selection", lambda: {"parts": ["U1"]})
        run.run("mechanical_fabrication", lambda: {"shape": "box"})
        run.run("cad_generation", lambda: {"step": "assembly.step"})
        run.run("package_project", lambda: {"ready": True})

        invalidated = run.invalidate("mechanical_fabrication")

        self.assertIn("mechanical_fabrication", invalidated)
        self.assertIn("cad_generation", invalidated)
        self.assertIn("package_project", invalidated)
        self.assertNotIn("component_selection", invalidated)
        self.assertEqual(
            GenerationStageStatus.SUCCEEDED,
            run.records["component_selection"].status,
        )
        self.assertEqual(
            GenerationStageStatus.NOT_STARTED,
            run.records["cad_generation"].status,
        )

    def test_approval_gate_is_persisted_and_resumable(self) -> None:
        run = GenerationStageRun(
            "default",
            [
                GenerationStageSpec(stage_id="system_visual"),
                GenerationStageSpec(
                    stage_id="visual_acceptance",
                    dependencies=["system_visual"],
                    approval_gate=True,
                ),
                GenerationStageSpec(
                    stage_id="cad_generation",
                    dependencies=["visual_acceptance"],
                ),
            ],
        )
        run.run("system_visual", lambda: {"url": "forma://visuals/system.png"})
        run.wait_for_approval("visual_acceptance", output={"visual": "system"})
        run.run("cad_generation", lambda: {"step": "assembly.step"})

        self.assertEqual("waiting_for_approval", run.overall_status)
        self.assertEqual(
            GenerationStageStatus.WAITING_FOR_APPROVAL,
            run.records["visual_acceptance"].status,
        )
        self.assertEqual(
            GenerationStageStatus.BLOCKED,
            run.records["cad_generation"].status,
        )

        run.approve("visual_acceptance", output={"approved": True})
        run.invalidate("cad_generation")
        result = run.run("cad_generation", lambda: {"step": "assembly.step"})

        self.assertEqual({"step": "assembly.step"}, result)
        self.assertEqual(GenerationStageStatus.SUCCEEDED, run.records["cad_generation"].status)

    def test_pipeline_metadata_exposes_cost_to_clients(self) -> None:
        steps = {item["id"]: item for item in list_agent_pipeline_steps("default", include_image=True)}
        self.assertEqual("semantic", steps["system_architecture"]["fidelity"])
        self.assertEqual("assembly_cad", steps["cad_generation"]["fidelity"])
        self.assertEqual("visual", steps["image_generation"]["fidelity"])
        self.assertIn("compute", steps["cad_generation"]["cost"])


if __name__ == "__main__":
    unittest.main()
