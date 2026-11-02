# Minimal consumer example

This directory is a complete, runnable sample for the
[Continuum Quick Start](../../docs/quickstart.md).

- `app.py` and `tests/test_app.py` are the sample project.
- `.github/workflows/ci.yml` is the project-owned workflow named `CI`. It
  calls `kodmial/continuum/.github/workflows/continuum-validation.yml@main` and
  passes `continuum_ref: main`, keeping the consumer on the current Continuum
  main as the shared workflow and its engine evolve. It runs the sample build
  and tests without relying on auto-detection.
- `verification/test_ready_admission.py` is an offline fixture for the
  scheduler's admission behavior. It is not part of the consumer project and is
  not run by the sample `ci.yml`.

For this current-main consumer profile, keep both references on `main`
and install the shared actions from `main` as well. Do not pin the consumer to
a transient PR branch or review commit.
