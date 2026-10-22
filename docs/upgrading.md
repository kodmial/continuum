# Upgrading, pinning, and rollback

Continuum is consumed as `workflow_call` workflows. There is no package to
upgrade with a package manager and this guide does not create releases, tags, a
changelog, or new version pins. The installer is the supported update/rollback
tool.

## What the revision controls

Every installed caller contains a Continuum `uses:` line naming one revision.
Callers that also run fallback engine scripts carry a matching
`continuum_ref`/`engine_ref` value; the installer writes the requested ref to
both, so the workflow and the scripts it resolves use the same revision. The
metadata-only child run-cleanup caller has no engine ref because it executes no
fallback script.

- `@main` means the live, moving shared implementation. It receives fixes
  without a consumer change, but the same caller can behave differently between
  runs.
- A full commit SHA is the reproducible choice. It freezes the workflow and its
  fallback scripts (where present) until the consumer reinstalls at another SHA.
- A branch or tag is also accepted by the installer, but a mutable branch/tag
  does not give the reproducibility of a commit SHA.

The installer manages `.github/workflows/continuum-*.yml` only. It does not own
the consumer's project `ci.yml`; if that file pins
`continuum-validation.yml@main`, update that `uses:` ref separately. Keeping the
caller and project CI on the same revision avoids mixing engine interfaces.

## Install sets are independent

| Set | What it adds |
| --- | --- |
| `core` | The task-domain lifecycle callers (scheduler, OpenCode, review providers, auto-merge, qualification controllers). It is the default set. |
| `tech` | The opt-in technology library, currently the Swift CI caller. |
| `parent` | The delegated child worker, review, PR review, and run-cleanup callers. The ordinary scheduler comes from `core`. |

`core` does not install `tech` or `parent`. Re-run every set the repository
uses, and only those sets, when changing revisions.

## Upgrade procedure

1. Record the revision currently in use (for example the SHA in an installed
   caller or the deployment change that set it).
2. Choose the target revision. Prefer a tested full commit SHA for production;
   keep `main` only if the consumer deliberately tracks the live branch.
3. Preview the change. `--dry-run` prints the exact `add`, `update`, `keep`,
   `ownership-conflict`, and `prune` plan and changes nothing:

   ```sh
   bash install.sh /path/to/consumer <target-ref> core --dry-run
   bash install.sh /path/to/consumer <target-ref> tech --dry-run   # if installed
   bash install.sh /path/to/consumer <target-ref> parent --dry-run # if installed
   ```

4. Apply the same commands without `--dry-run`. An explicit ref fetches that
   revision's templates over the network. From a local checkout, an install
   with no explicit ref uses the local `main` working-tree templates instead.
5. Update the project-owned `ci.yml` Continuum `uses:` ref if it is pinned
   separately.
6. Inspect the resulting diff and let the consumer's normal CI validate it.
   Do not hand-patch an installed `continuum-*.yml`; change the consumer's
   variables/inputs or the project-owned workflow instead.

The installer is idempotent: an identical reinstall keeps files byte-for-byte,
preserves project-owned workflows, and does not duplicate the CI caller.
Overwrites and prunes are transactional with rollback for errors the process
can observe. See [Installer safety](installer-safety.md).

## Rollback procedure

Rollback is an upgrade to an older revision, not a separate code path:

1. Stop automation that could publish new work (remove the ready label, pause
   the scheduler, or otherwise use the consumer's normal controls).
2. Run the same install sets at the previous full commit SHA.
3. Update the project-owned CI ref to that SHA.
4. Re-enable automation and confirm the expected workflow names resolve.

Roll back **all** sets together. Mixing revisions can combine a caller with an
engine interface it was not written for. If the chosen rollback target predates
the current `continuum-` workflow naming generation, use the installer from that
target revision rather than the current installer; the current templates and
stored names may not exist there.

Continuum cannot make an old caller understand a newer interface. A rollback
that crosses a changed input, label, marker, or workflow name is supported only
when the installer and callers from the target revision agree as a set.

## Superseded-caller cleanup

Reinstalling can remove a caller that the selected revision no longer ships.
This is the only destructive action the installer takes, so it is fenced:

- **Ownership** — a file is a candidate only when it has the `continuum-`
  prefix **and** its body references this repository. A consumer's own
  `continuum-experiment.yml` is never a candidate.
- **Confirmation** — at a terminal the exact list is printed and confirmation is
  required. A non-interactive install deletes nothing; it only reports what it
  would remove.
- **Explicit opt-in** — `--yes` or `CONTINUUM_INSTALL_ASSUME_YES=1` approves the
  prune unattended. `--yes` may appear in any argument position.
- **Self-install** — the prune is skipped when the target is Continuum's own
  checkout.

Preview stale cleanup before applying it:

```sh
bash install.sh /path/to/consumer <target-ref> core --dry-run --yes
bash install.sh /path/to/consumer <target-ref> core --yes
```

`--dry-run` changes nothing even when `--yes` is present. An ownership conflict
(a directory, symlink, or non-Continuum file at a caller path) fails closed
before any write rather than overwriting the file.

## Compatibility cautions

- **Workflow names are interface.** Controllers and `workflow_run` filters match
  stable names such as `CI`, `OpenCode agent`, and `Subtask`. Do not rename the
  consumer's project CI or an installed caller.
- **Labels and markers are coupled.** The scheduler, OpenCode executor, and
  watchdog must use the same `AUTOMATION_*` markers and labels. Change them
  together, or leave the defaults.
- **The old set name is gone.** `swift` is rejected; use `tech`. Older installs
  that relied on the former default should run `core` and, if wanted, `tech`
  explicitly.
- **The old provider switches are gone.** Use
  `CONTINUUM_REVIEW_PROVIDER=none|coderabbit|pr-agent`. Deprecated
  `require_coderabbit` inputs are compatibility no-ops.
- **Old issue ownership markers are not routing.** Child routing is controlled
  by `CONTINUUM_ROLE` and the parent/child variables, not by issue-body markers.
- **Credentials are contracts.** `TAP_PAT` remains a classic PAT with `repo` and
  `workflow` scopes; `RENDER_API_KEY` is a separate Render credential. A GitHub
  token is not a Render API key.
- **Free-model behavior is not a compatibility guarantee.** Model routes,
  quotas, and availability are external. Recovery is bounded and can end in a
  pause instead of a successful run.

Current consumers configured with `main` should keep using `main` unless they
explicitly choose reproducibility. No release, tag, SemVer bump, changelog, or
consumer upgrade is initiated by this document.
