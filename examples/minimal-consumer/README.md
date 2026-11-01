# Minimal consumer example

This directory is a complete, runnable sample for the
[Continuum Quick Start](../../docs/quickstart.md).

- `app.py` and `tests/test_app.py` are the sample project.
- `.github/workflows/ci.yml` is the project-owned workflow named `CI`. It pins
  `kodmial/continuum/.github/workflows/continuum-validation.yml` and its
  `continuum_ref` input to the same reviewed commit, and runs the sample build
  and tests without relying on auto-detection.
- `verification/test_ready_admission.py` is an offline fixture for the
  scheduler's admission behavior. It is not part of the consumer project and is
  not run by the sample `ci.yml`.

For a reproducible install, keep both references in `ci.yml` on the same
reviewed Continuum revision you pass to `install.sh`; never copy them as a
mutable branch name.
