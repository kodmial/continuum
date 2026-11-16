# Minimal consumer example

This directory is a complete, runnable sample for the
[Continuum Quick Start](../../docs/quickstart.md).

- `app.py` and `tests/test_app.py` are the sample project.
- `.github/workflows/ci.yml` is the project-owned workflow named `CI`. As
  committed here it deliberately uses the moving `main` path: it calls
  `kodmial/continuum/.github/workflows/continuum-validation.yml@main` and
  passes `continuum_ref: main`, keeping the consumer on the current Continuum
  main as the shared workflow and its engine evolve. It runs the sample build
  and tests without relying on auto-detection.
- `verification/test_ready_admission.py` is an offline fixture for the
  scheduler's admission behavior. It is not part of the consumer project and is
  not run by the sample `ci.yml`.

## Choose one ref policy for a first run

The Quick Start recommends the **reproducible** path: pin the same full commit
SHA in the caller `uses:` ref, `continuum_ref:`, and the installer invocation.
Its rewrite script changes this example's `ci.yml` accordingly and fails if
either reference is missing, so the two cannot silently diverge.

This checked-in example uses the **deliberately moving** alternative:
`@main` plus `continuum_ref: main`. That is valid only when selected for both
references and when you accept that the workflow and its engine move together.
Do not mix a pinned `uses:` with an unpinned `continuum_ref:` (or the reverse),
and do not pin the consumer to a transient PR branch or review commit.
