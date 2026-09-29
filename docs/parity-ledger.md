# Parity ledger

Status: **normative for the P0 sweep**. Issue #59 asks for production parity
with the repositories Continuum was extracted from. This document explains what
the claim means, how it is recorded, and how it is checked.

The machine-readable source of truth is [`parity/ledger.v1.json`](../parity/ledger.v1.json).
`continuum parity-check` validates it, and CI runs that on every pull request.

## What the claim is

Parity is not "the same files". It is: **for every capability the source
repositories have, this repository either has it, has a better version of it, or
has recorded why it is deliberately not Continuum's job** — and the reason is
one of five fixed values.

A capability that nobody looked at is not absent by decision; it is unknown, and
unknown is what a parity claim cannot contain.

## The five dispositions

| Disposition | Meaning |
| --- | --- |
| `absorbed` | The behavior is in Continuum. The entry names the module or path that carries it. |
| `must-port` | A real gap that Continuum owes. Any entry with this disposition fails P0. |
| `consumer-local` | Correctly not in Continuum: it belongs to the consumer, or to a product the source repository owns. |
| `superseded` | The source's version was replaced by something better, and the better one is what Continuum has. |
| `not-applicable` | Out of scope for this contract entirely. |

There is no "considered", no "later", and no "probably fine". An entry that
needs a follow-up names the trigger for it in `notes`, but its disposition still
has to be true today.

## What the validator enforces

`continuum parity-check` fails closed on every one of these, and collects all
problems before reporting so a fix is not a sequence of one-error rounds:

- The version is `ledger.v1`.
- `audited_at` is a **full 40-character commit sha**, not a date. The audit is a
  statement about specific code; a date would let the code change underneath it.
- Every entry has an id of the form `AA-01`, a non-empty capability, one of the
  five dispositions, a known repository, and a full commit sha as its
  `source_ref`. A branch or tag reference is refused, for the same reason.
- `absorbed`, `must-port`, and `superseded` entries name the `module` or `paths`
  that carry the behavior, and that module or path is verified to exist.
- `consumer-local` and `not-applicable` entries carry a `rationale`.
- Ids are unique.
- Every audited repository has at least one entry. `kodmaiadmin` is the one
  documented exception: it is pinned to an old sha and carries no parity
  evidence, so its absence is asserted in the test suite rather than left
  ambiguous.

`check()` then reports the questions a ledger cannot answer alone: whether any
`absorbed` entry names a module that is not in the tree, and whether a sweep
that claims to have ported nothing and already had everything is plausible. It
is not.

## Repositories under audit

| Repository | Snapshot | Notes |
| --- | --- | --- |
| `kodmial/nanodictate` | `54db335ea7c03d35ee3a32a9b5d12ed9a6ef01aa` | Source of the review plane and the release plane. |
| `kodmial/runtime-lab` | `b50a23d6ee76a597feb3584899f89acac91cfece` | Source of the credential guard and the run-materialization flow. |
| `kodmial/kodmai` | `3c64731ec888f8eb90395a3b24fd8f44d23cae50` | First consumer of the recovery plane. |
| `kodmial/opencode` | `8b9fc17f820dd01ed6c919a738615f5240b6bbe8` | First consumer entry workflow. |
| `kodmial/homebrew-nanodictate` | `29bb895f8142249d817f2a60f5743f78a9b6ac35` | Packaging manifests; consumer-owned. |
| `kodmial/macports-nanodictate` | `74ee768a6cc9dc0211c34e1dee912e7e35343c50` | Packaging manifests; consumer-owned. |
| `kodmial/kodmaiadmin` | `cbab80796dac70883396dbfac3b96d66b8ffe524` | Pinned to an old sha; not parity evidence. |

The immutable workflow snapshot that accompanied this audit is
[`reference/nanodictate-workflows/`](../reference/nanodictate-workflows/README.md),
taken at `64e89a3bcbab671513a933855e7495a07fac56bb`. It is reference material
only: it lives outside `.github/workflows` so GitHub Actions cannot execute it.

## How an entry is written

```json
{
  "id": "ND-06",
  "capability": "Every review-ready pull request gets its first full review before another pull request consumes a second shared quota slot",
  "repository": "nanodictate",
  "disposition": "absorbed",
  "source_ref": "e5f9a84aa595d24d51b0900a0a2c8eadf5a2c66b",
  "module": "continuum.review.queue",
  "rationale": "rank_key() orders by priority and then by the number of full reviews the provider already spent on the pull request, counted over its whole history, and the queue log reports that count."
}
```

`capability` states the behavior in terms that do not mention an implementation,
because the implementation is what the `module` field is for. `rationale` is
what a reviewer checks: it must name the specific thing in Continuum that
delivers the behavior, not the fact that the behavior exists.

## Adding an entry

1. Read the capability out of the source repository at a **full commit sha**.
2. Decide the disposition, and write the reason Continuum's current state is
   correct.
3. If it is `absorbed`, name the module or path. If it is not, say why in
   `rationale`.
4. Run `continuum parity-check` and `python3 -m unittest tests.test_parity`.

An entry is added when a capability is discovered, and its disposition is
changed when Continuum's state changes. Editing an entry to match new code is
the point; editing one to match a preferred answer is not.
