# AGENTS.md

Instructions for AI coding agents working in this repository.

## App version bump (required on every PR to main)

The Home Assistant Supervisor only offers an app update when the `version`
field in `crealipen/config.yaml` changes. Every PR that lands on `main`
must therefore bump that version, following semantic versioning:

- **Patch** (`0.2.1`): bug fixes only — no new UI or API surface.
- **Minor** (`0.3.0`): new features — new UI panels, endpoints, or settings.
- **Major** (`1.0.0`): breaking changes to stored Drawings, settings, or the API.

Bump the version in the same PR as the change it ships. CI fails any PR or
push to `main` where the version did not increase.

## Workflow

Feature branches off `main`, pull requests, green CI before merge — see
[README.md](./README.md#contributing). Tests are written before the code
that satisfies them (TDD); backend tests live in `backend/tests/`.
