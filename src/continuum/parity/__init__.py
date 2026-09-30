"""Cross-repository parity: what Continuum audited upstream, and what drifted since.

``docs/parity-ledger.md`` is the human account of the sweep. This package is the
same account in a form a program can check, and it is split so that each question
has exactly one owner:

* :mod:`~continuum.parity.ledger` -- what was audited, and what was concluded. No
  network, no opinions; a strict reader for the committed ledger plus any
  uncommitted overlay.
* :mod:`~continuum.parity.sources` -- what is true of each source right now. The
  only part that touches the network, and the only part that sees a credential.
* :mod:`~continuum.parity.drift` -- whether an advance is drift, and at what
  priority.
* :mod:`~continuum.parity.issue` -- one deduplicated issue per drifting source.
* :mod:`~continuum.parity.promote` -- whether a consumer pin may move, and the
  claim it makes.
* :mod:`~continuum.parity.cli` -- the five subcommands, one per question.

Nothing here copies code between repositories. Every module in this package
takes a commit, a path, or a pull request number as evidence and stops there; a
finding is a request to classify, and the five dispositions are what a
classification may say.

Only the names that do not collide with a submodule are re-exported here. ``load``,
``compute``, ``plan`` and ``promote`` are each both a module and a function, and a
package that binds the function over the module makes ``from . import promote``
resolve to the wrong object -- which is a confusing failure for whoever imports
this next.
"""

from . import drift, issue, ledger, promote, sources
from .drift import DriftReport, PathVerdict, SourceDrift
from .issue import IssuePlan
from .ledger import Ledger, LedgerError, Problem, Source
from .promote import (
    NO_UNCLASSIFIED_P0_CLAIM,
    PromotionError,
    PromotionVerdict,
)
from .sources import (
    GitHubSourceReader,
    RedactionError,
    SourceAccessError,
    SourceObservation,
    assert_public,
)

__all__ = [
    "DriftReport",
    "GitHubSourceReader",
    "IssuePlan",
    "Ledger",
    "LedgerError",
    "NO_UNCLASSIFIED_P0_CLAIM",
    "PathVerdict",
    "Problem",
    "PromotionError",
    "PromotionVerdict",
    "RedactionError",
    "Source",
    "SourceAccessError",
    "SourceDrift",
    "SourceObservation",
    "assert_public",
    "drift",
    "issue",
    "ledger",
    "promote",
    "sources",
]