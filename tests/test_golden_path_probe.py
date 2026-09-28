"""Golden-path probe for the autonomous issue lifecycle.

This file is the observable half of a stabilization probe. The control plane
claims it can take an issue from the scheduler reservation all the way to a
merged PR and a closed issue without a human touching the keyboard. This test
is the artifact that claim leaves behind: it only passes if the whole chain
actually ran and delivered it.

The test is intentionally trivial. It carries no production behaviour and
needs no network, no fixture and no clock, so the only thing it can fail on is
the lifecycle itself. A probe that can fail for ordinary reasons is a probe
that cannot be trusted to report a stabilization failure, so this stays a
single deterministic assertion about a literal marker whose only job is to be
carried, unchanged, from the dispatch to the merge.

Never soften this assertion to make a red run go away. A weakened probe is a
stabilization failure recorded as a pass.
"""

from __future__ import annotations

import pathlib
import unittest

# The marker this probe carries through the lifecycle.
MARKER = "continuum-golden-path-v1"

# The version the marker is expected to still hold. It is a second, literal
# spelling rather than a reference to MARKER: comparing the declared marker
# against itself would pass even after the marker had been silently re-versioned,
# which is exactly the failure this probe exists to catch.
EXPECTED = "continuum-golden-path-v1"

DECLARATION_PREFIX = "MARKER = "
SOURCE = pathlib.Path(__file__).resolve()


class GoldenPathProbeTests(unittest.TestCase):
    def test_golden_path_marker_is_unchanged(self):
        source = SOURCE.read_text(encoding="utf-8")
        declared = [
            line[len(DECLARATION_PREFIX) :].strip().strip('"')
            for line in source.splitlines()
            if line.startswith(DECLARATION_PREFIX)
        ]
        self.assertEqual(declared, [EXPECTED])


if __name__ == "__main__":
    unittest.main()
