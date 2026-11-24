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

## Shipping to Home Assistant

The app image is built by CI (`publish.yml`) on every merge to `main` and
published to GHCR, tagged with the `version` from `crealipen/config.yaml`;
the Supervisor pulls `ghcr.io/bmcandy/3d-plotter/crealipen-{arch}` at that
tag (see [ADR 0005](./docs/adr/0005-prebuilt-ghcr-images.md)). Shipping a
change therefore means bumping the version in the same PR as the change —
there is no committed build output to sync.

## Workflow

Feature branches off `main`, pull requests, green CI before merge — see
[README.md](./README.md#contributing). Tests are written before the code
that satisfies them (TDD); backend tests live in `backend/tests/`.
