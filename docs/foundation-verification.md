# Foundation verification — 2026-11-04

Supported source pair: Form **0.3.7** and Open-CAD **0.2.5**, pinned to
`89c85423b831f2a5b66d21d2087b7eb45d113585` in the native adapter and CI.
The CAD change is [Open-CAD PR #129](https://github.com/caid-technologies/Open-CAD/pull/129).
This release implements inspectable artifacts, deterministic boundaries, human
authority and open interfaces as the normal hardware workflow. It adds no new
application domains.

## Executed local checks

Environment: Python 3.12.6 on Intel macOS; CadQuery 2.8.0, OCP 7.9.3.1.1,
NumPy 2.2.6 and the native constraints committed with this change.

| Check | Result |
| --- | --- |
| Full Form pytest suite, default environment | 1,122 passed; 277 subtests passed; eight native/environment gates skipped |
| Full offline Form unittest discovery | 1,043 passed; seven native gates skipped |
| Native Form acceptance | Real create/compile/review/export/reopen/edit/recompile passed, including 40 mm → 60 mm measured STEP geometry, stale reviews and retained attachments |
| Final native workflow/API/inspection regression | 28 passed; 12 subtests passed; zero skips |
| Final shared-boundary regression after generic CAD invalidation guard | 31 passed; nine subtests passed; native canonical case covered separately |
| Native canonical source persistence | Passed, including deletion of temporary worker files followed by durable inspection and portable reload |
| Existing complex native assembly | Passed after preserving independent bodies; exact body count and valid geometry checked |
| Component assets/storage/MCP/schema regression | 36 passed; actual native STEP import executed |
| Independent artifact API integrity regression | Six passed |
| Final installed Open-CAD wheel contract/native checks | 22 passed; zero skips |
| Open-CAD installed-core suite before final dormant-root guard | 714 passed; 14 optional tests skipped; final guard covered by rerun contract tests |
| Open-CAD full native no-skip gate | 280 passed; zero skips or known defects |
| Built Form wheel and source distribution | Passed, including packaged canonical CAD adapter |
| Isolated installed Form/Open-CAD wheel workflow | Passed; imports resolve installed distributions, editable checkouts are absent from discovery |
| Dependency consistency and whitespace checks | Passed |

The native full Form unittest run originally exposed one disjoint-body fusion
failure in an existing assembly fixture. The generator now retains those parts
as independent assembly bodies, and that exact native fixture passed with
geometry validity and body-count assertions. The offline full suite and focused
native cases were rerun after the fixes; skipped default-environment tests are
not counted as native evidence.

## Scope of evidence

The acceptance fixture is explicitly authored typed geometry, not a claim of
live-model generation accuracy. Independent STEP import checks actual exported
dimensions and geometry. Hashes demonstrate byte integrity; neither valid JSON,
native compilation nor a caller-declared review establishes fit, load capacity,
manufacturability, regulatory compliance or physical qualification.

CI independently builds and exercises the exact source pair on Linux and
Windows. See the PR checks for that platform evidence; local sharing-failure
regressions alone are not represented as actual Windows execution.

## Deployment and migration

Apply `supabase/migrations/20261004191454_component_asset_library.sql` before
deploying the new asset API against hosted Supabase. SQLite initializes the
metadata through the existing application schema. Native authoring requires the
pinned Open-CAD runtime and durable private artifact storage.

This session did not apply a hosted database migration or qualify physical
hardware. Existing project formats remain readable; unsupported versions,
unverified legacy CAD evidence, stale geometry and invalid artifact bytes are
reported explicitly. Portable generation history is evidence, not an execution
cache. Instructions and the precise public contracts are in
[Project foundation](project-foundation.md).
