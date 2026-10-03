# Quality evaluations

This directory is reserved for suites that score generated hardware output rather than runtime performance. Candidate dimensions include BOM accuracy, component compatibility, wiring correctness, prompt adherence, schema completeness, hardware feasibility, and hallucinated or unsupported components.

Each suite should identify its dataset and rubric, emit machine-readable results, and distinguish deterministic checks from opt-in live-provider runs.

## Context-source comparison

`compare_context_sources.py` is an opt-in live-provider evaluation that runs the same project prompt through Web Research and Past Jobs context, using OpenAI GPT-5.5 for generation and GMI `gpt-image-2` for product imagery. It records wall-clock and persisted job timings, deterministic HardwareIntermediateRepresentation metrics, independent OpenAI multimodal quality reviews, and a pairwise quality judgment.

```bash
apps/api/.venv/bin/python evals/quality/compare_context_sources.py
```

Reports are written under `.logs/context-source-eval/`; credentials are loaded from `.env` and never written to the report.
