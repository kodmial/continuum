# Minimal consumer example

This directory is a complete, runnable sample for the
[Continuum Quick Start](../../docs/quickstart.md).

- `app.py` and `tests/test_app.py` are the sample project.
- `.github/workflows/ci.yml` is the project-owned workflow named `CI`. As
  committed here it deliberately uses the moving `main` path: it calls
  `kodmial/continuum/.github/workflows/continuum-validation.yml@main` and
  passes `continuum_ref: main`. For `continuum-validation.yml` the revision is
  determined only by the caller's `uses:` ref; `continuum_ref:` has no effect
  there, although the installed callers do read it to select their engine
  checkout. It runs the sample build and tests without relying on
  auto-detection.
- `verification/test_ready_admission.py` is an offline fixture for the
  scheduler's admission behavior. It is not part of the consumer project and is
  not run by the sample `ci.yml`.

## Choose one ref policy for a first run

The Quick Start recommends the **reproducible** path: pin the same full commit
SHA in the installer invocation and the caller `uses:` ref. For callers that
read `continuum_ref:`, including the installed core callers, keep that input on
the same revision too; the sample `ci.yml` passes it as well even though
`continuum-validation.yml` ignores it. The Quick Start's rewrite script updates
both references in this example's `ci.yml` and fails if either is missing, so
the sample stays internally consistent.

This checked-in example uses the **deliberately moving** alternative:
`@main` plus `continuum_ref: main`. For `continuum-validation.yml` the `uses:`
ref alone selects the workflow, so `continuum_ref: main` is inert there. For
callers that read the input, mixing a pinned `uses:` with an unpinned
`continuum_ref:` (or the reverse) is invalid. Do not pin the consumer to a
transient PR branch or review commit.
