# Prebuilt GHCR images instead of Supervisor builds

Date: 2026-11-24

## Status

Accepted

## Context

ADR 0004 wrapped the standalone app as a locally-built HA app: the
Supervisor builds the image from `crealipen/` as its build context, which
forced a committed deployment copy (the compiled frontend plus a copy of
the backend) that was synced by hand. The copy drifted — v0.3.1 shipped
old code to the Pi despite a version bump — and a CI drift check only
makes the drift loud; the duplicate remains.

## Decision

CI builds the app image on every merge to `main` (repository root as
build context, amd64 and aarch64) and pushes it to GHCR, tagged with the
app version. `crealipen/config.yaml` points at
`image: ghcr.io/bmcandy/3d-plotter/crealipen-{arch}`, so the Supervisor
pulls the image instead of building it. The deployment copy, the sync
script, and the drift check are deleted.

## Consequences

- One source of truth: `backend/` and `frontend/` are the only sources to
  keep current; shipping a change is a version bump.
- The repo must stay public — GHCR images inherit its visibility.
- A merge must be followed by a successful `publish.yml` run before the
  new version is updatable in HA; the version-check workflow still
  requires the version to increase on every PR.
- Local and standalone Docker deployments keep using the root Dockerfile.
