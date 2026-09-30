"""Provenance: the repositories Continuum stays in parity with, and the drift
between what they do and what this repository claims about them.

``docs/parity-ledger.json`` answers "which of the consumer's workflows did we
audit, and at which blob". This package answers the question that outlives that
one: "has the repository we took those claims from moved since, and does its
movement matter". The two are read together -- a source here points at the
workflow ledger that classifies its paths -- and neither replaces the other.

The plane never copies code from a source. It records what moved, and it refuses
to let a consumer pin be promoted over an advance nobody has classified.
"""