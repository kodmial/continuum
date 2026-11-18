# Release templates (ARCHIVED / INACTIVE / NOT QUALIFIED)

These files are reference material, not a working release feature. They are not
under `.github/workflows/`, they are not installed by `install.sh`, and no
trigger or publication can run from this directory. The generic release engine
and release CLI do not exist on current main.

Read [docs/release-templates.md](../../docs/release-templates.md) for the
architecture, provenance, current gaps, and the prerequisites for qualifying a
real release later.

| File | Kind | Purpose |
| --- | --- | --- |
| `generic-release-workflow.yml` | Historical reusable workflow | Multi-target release orchestration recovered from `0bbf57dbb8834d0aaef2cfb83f08ead7da623985`. `workflow_call` only. |
| `consumer-release-handoff.yml` | Historical consumer workflow | Thin, deliberately empty hand-off entrypoint. |
| `bun-binary-release-workflow.yml` | Historical optional workflow | Separate Bun binary example; not the default architecture. |
| `macos-release-caller.yml` | Illustrative caller workflow | Manual selection that hands `version` and `source_sha` to the generic workflow. |
| `macos-target.continuum.yml` | Illustrative policy/config | SwiftPM macOS `.app` target for `arm64` and `x86_64`; **not** a workflow. |
| `macos/Info.plist` | Illustrative input | Neutral bundle metadata; the historical adapter stamped the built bundle's own version keys, not this configured path. |
| `macos/ExampleApp.entitlements` | Illustrative input | Neutral, empty entitlement example. |

Every product name, identifier, identity, and path is a placeholder. Replace
them with your own values only after the missing engine, adapter, and
qualification work is complete. The target example declares `homebrew` and
`macports` as downstream destinations only; no publisher is included.

Do not copy `macos-release-caller.yml` into `.github/workflows/` and expect a
release. The generic release workflow it calls is not active on current main,
and the historical Apple adapter was a signing/verification plan without a
walkable release adapter. See the main documentation for the exact obstacles.
