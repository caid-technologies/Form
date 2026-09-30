# Contributing to Forma

Thank you for your interest in contributing to Forma.

Forma is an AI-native hardware design platform that converts natural-language requirements and optional reference images into structured Hardware Intermediate Representation, bills of materials, wiring diagrams, validation results, assembly instructions, and visual representations.

The project is currently an alpha-stage research prototype. Contributions should prioritize correctness, maintainability, testability, and safe low-voltage hardware design.

## Ways to Contribute

You can contribute by:

* Fixing bugs
* Improving documentation
* Adding or improving tests
* Improving the frontend experience
* Extending Hardware Intermediate Representation models
* Improving electrical validation
* Adding supported LLM or image providers
* Improving project iteration and self-correction
* Improving observability, persistence, or deployment support
* Adding examples or evaluations
* Reviewing issues and pull requests

Small, focused contributions are welcome.

## Project Scope and Safety

Forma currently focuses on educational and maker-oriented low-voltage electronics, primarily 3.3V–5V DC systems.

Contributions must preserve the project’s safety boundaries. Features that generate, validate, or modify hardware designs should continue to block or clearly warn about unsupported high-risk domains, including:

* Mains electricity
* Medical devices
* Automotive control systems
* Weapons
* High-power battery systems
* Other designs where failure could cause serious injury or property damage

Changes to validation behavior should include tests demonstrating both accepted and rejected designs.

See [`docs/validation.md`](docs/validation.md) for the current validation model.

## Before Starting

For substantial changes, open an issue before writing the implementation. This helps confirm that the proposal fits the project’s direction and prevents duplicated work.

An issue should explain:

* The problem being solved
* The proposed behavior
* The affected part of the system
* Any API, schema, database, or deployment changes
* Alternatives considered
* Known safety implications

Small documentation corrections and narrowly scoped bug fixes may be submitted directly.

## Development Workflow

Forma uses the following branch progression:

```text
feature branch → dev → staging → main
```

The branches serve different purposes:

* `dev` integrates completed development work.
* `staging` is used to test a release candidate containing multiple integrated changes.
* `main` contains production-ready releases.

Do not open feature pull requests directly against `staging` or `main`.

External contributors should fork the repository and open pull requests against `dev`.

### Create a Fork and Local Clone

```bash
git clone git@github.com:YOUR_USERNAME/Forma-OSS.git
cd Forma-OSS

git remote add upstream git@github.com:caid-technologies/Forma-OSS.git
git fetch upstream
```

Create your branch from the latest `dev` branch:

```bash
git switch dev
git pull upstream dev

git switch -c feature/short-description
```

Recommended branch prefixes include:

```text
feature/
fix/
docs/
test/
refactor/
chore/
```

Examples:

```text
feature/add-provider-health-check
fix/project-iteration-history
docs/update-local-setup
test/validation-overcurrent-cases
```

Keep each branch focused on one logical change.

## Local Development Setup

### Prerequisites

You will need:

* Python 3.11 or newer
* Node.js 18 or newer
* npm
* Git
* Docker and Docker Compose, optionally

Supabase and live model-provider credentials are optional for most local development. Forma can use SQLite and simulation mode locally.

### Environment Configuration

Copy the example environment file:

```bash
cp .env.example .env
```

Never commit API keys, Supabase credentials, encryption keys, access tokens, or other secrets.

Use simulation mode or local development settings unless your change specifically requires a live service.

### Backend

From the repository root:

```bash
python3 -m venv .venv
source .venv/bin/activate

python -m pip install --upgrade pip
pip install -r apps/api/requirements.txt
```

Run the backend:

```bash
uvicorn apps.api.main:app --reload --port 8000
```

The API documentation will be available at:

```text
http://localhost:8000/api/docs
```

For changes to the reusable Python package, install it in editable mode with development dependencies:

```bash
pip install -e ".[dev]"
```

### Frontend

In a separate terminal:

```bash
cd apps/web
npm install
npm run dev
```

The frontend will be available at:

```text
http://localhost:3000
```

### Run the Full Application

From the repository root:

```bash
./scripts/development/dev.sh
```

On Windows PowerShell:

```powershell
.\scripts\development\dev.ps1
```

This starts the FastAPI backend and Next.js frontend together.

### Docker

To run the complete application through Docker:

```bash
docker compose up --build
```

By default, the backend runs on port `8000` and the frontend runs on port `3000`.

## Repository Structure

Important directories include:

```text
apps/api/          FastAPI application and service integrations
forma_core/   Reusable generation, validation, provider, and project logic
apps/web/         Next.js frontend
tests/            Concern-grouped offline Python unit tests (see tests/README.md)
evals/            Performance benchmarks, quality evaluations, datasets, and reports
docs/             Architecture and development documentation
scripts/          Concern-grouped developer and operational tools (see scripts/README.md)
supabase/         Supabase schema and migration resources
examples/         Example inputs and generated outputs
```

New reusable domain logic should generally be added to `forma_core`.

Avoid adding new business logic to legacy compatibility wrappers when an appropriate module exists under `forma_core`.

## Code Standards

### Python

Python contributions should:

* Use Python type annotations
* Include docstrings for public classes and functions
* Use Pydantic models for structured domain data
* Keep functions focused and independently testable
* Avoid hidden global state
* Use clear exception types and actionable error messages
* Preserve compatibility with Python 3.11
* Avoid network access in unit tests
* Avoid logging secrets or complete provider responses containing sensitive data

Public APIs should use explicit input and return types.

Example:

```python
def validate_project(project: HardwareIntermediateRepresentation) -> list[ValidationIssue]:
    """Validate a hardware project and return discovered safety issues."""
```

### Frontend

Frontend contributions should:

* Use TypeScript
* Avoid unnecessary `any` types
* Keep components focused
* Preserve responsive behavior
* Handle loading, empty, success, and error states
* Avoid exposing server-only environment variables to the browser
* Use existing design and interaction patterns where possible

UI changes should include screenshots or a short recording in the pull request.

### Hardware Intermediate Representation and APIs

Changes to Hardware Intermediate Representation models or API responses require additional care.

When modifying a schema:

* Explain the compatibility impact
* Update affected serializers and consumers
* Add migration or normalization behavior where practical
* Update examples and documentation
* Add tests for old and new representations
* Avoid silently removing existing fields

Breaking schema changes should be discussed in an issue before implementation.

### Provider Integrations

Provider integrations should:

* Use the existing provider abstraction
* Apply explicit timeout behavior
* Return normalized structured output
* Avoid provider-specific logic in unrelated modules
* Redact credentials and sensitive request data
* Include clear configuration errors
* Support offline testing through mocks or simulation
* Avoid making paid provider calls during the normal unit-test suite

Any live verification commands and their expected cost implications should be documented in the pull request.

### Database Changes

Database changes should include:

* The appropriate migration or schema update
* A description of upgrade behavior
* Any required environment changes
* Tests for persistence behavior where practical
* Safe handling of existing records

Do not assume that every developer has a configured Supabase project. Local SQLite behavior should remain functional unless the proposed change explicitly changes that policy.

## Testing

Run the offline Python test suite from the repository root:

```bash
./scripts/quality/test.sh
```

This performs Python compilation checks and runs the unit tests under `tests/`.

For frontend changes, run:

```bash
cd apps/web
npm run lint
npm run build
```

For Python package changes, also run:

```bash
python -m build
```

Add or update tests when changing:

* Hardware Intermediate Representation models
* Validation rules
* Project iteration
* Self-correction behavior
* Provider selection
* Serialization
* Persistence
* API behavior
* Error handling

Live provider tests should not replace deterministic offline tests.

## Commit Messages

Use concise commit messages that describe the result of the change.

Preferred prefixes include:

```text
feat: add configurable provider timeout
fix: preserve project history during iteration
docs: document SQLite development mode
test: add voltage mismatch validation cases
refactor: move generation logic into forma_core
chore: update frontend dependencies
```

Avoid commit messages such as:

```text
changes
fix stuff
updates
work
```

Clean up temporary or unrelated commits before requesting final review when practical.

## Pull Requests

A pull request should:

* Target `dev`
* Address one logical change
* Link its related issue
* Explain the problem and solution
* Describe how the change was tested
* Identify configuration or migration requirements
* Include screenshots for visible UI changes
* Include tests for new behavior
* Update documentation when behavior changes
* Avoid unrelated formatting or refactoring

Use closing keywords where appropriate:

```text
Closes #123
```

### Pull Request Checklist

Before requesting review, confirm that:

* [ ] The pull request targets `dev`
* [ ] The branch was created from an up-to-date `dev`
* [ ] The change is focused and reasonably sized
* [ ] Python code is typed
* [ ] Public classes and functions are documented
* [ ] Relevant tests were added or updated
* [ ] `./scripts/quality/test.sh` passes
* [ ] Frontend lint and build checks pass when applicable
* [ ] Documentation was updated when applicable
* [ ] No secrets or credentials were committed
* [ ] Logs, databases, build artifacts, and generated temporary files were not committed
* [ ] Hardware safety implications were considered
* [ ] Breaking changes are clearly identified

Maintainers may request changes before merging. A requested revision is part of the review process and not a rejection of the contribution.

## Reporting Bugs

Before opening a bug report, search existing issues to avoid duplicates.

Include:

* A clear title
* Steps to reproduce
* Expected behavior
* Actual behavior
* Relevant logs or screenshots
* Operating system
* Python version
* Node.js version
* Browser, when applicable
* Database backend
* Provider and model, when applicable
* The commit or version being used

Remove API keys, access tokens, project secrets, user data, and other sensitive information from logs.

## Feature Requests

Feature requests should describe the user problem before proposing an implementation.

Include:

* Who needs the feature
* The workflow it improves
* The expected behavior
* Why existing functionality is insufficient
* Safety or compatibility concerns
* A possible implementation, when known

## AI-Assisted Contributions

AI-assisted development is permitted, but contributors remain responsible for everything they submit.

Contributors must:

* Review and understand generated code
* Verify that the code works
* Add appropriate tests
* Check for fabricated APIs or dependencies
* Confirm that submitted material can legally be contributed
* Remove generated code that is unnecessary, insecure, or inconsistent with the project
* Disclose substantial AI-generated changes in the pull request when it helps reviewers evaluate the contribution

Generated output is not a substitute for technical review.

## Security Vulnerabilities

Follow the reporting instructions in [`SECURITY.md`](SECURITY.md) for suspected security vulnerabilities. Do not disclose a vulnerability in a public issue, discussion, or pull request.

## Licensing

Forma is distributed under the Mozilla Public License 2.0.

By submitting a contribution, you agree that your contribution may be distributed under the same license and that you have the right to submit it.

## Questions

For questions about a proposed contribution, open a GitHub Discussion or a focused issue with enough context for maintainers and other contributors to respond.
