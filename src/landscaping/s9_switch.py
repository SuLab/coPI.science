"""Whether S9, the final assessment, runs on this deployment (``LANDSCAPE_FINAL_ENABLED``).

On (the default) nothing changes. Off (the Scripps test stack, owner decision 2026-11-24:
Scripps does not evaluate opportunities the way the S9 rubric does):

- the worker never submits S9 and runs no S9 fallback (``worker``);
- a candidate activates once its post-drain cut's S6 and S8 have ended, labels included,
  without waiting for S9 or probes (``stages.final.candidate_ready``);
- a weekly batch takes never-issued opportunities in card order, the best member's S7
  position, without waiting for S9, and no batch is blind, so no probe is drawn
  (``review_batches``);
- the regression gate checks no kind that only S9 reads, and the baseline frozen at an
  activation takes its S6 items from the opportunities' members without S9 rows
  (``eval.regression``);
- the pages read no S9 row: a card is its S8 label, its S7 rank and its members' S6
  records and sources, and nothing says an assessment is missing (``landscape_view``).

S9 rows already stored are kept, unread; nor does the expert sample draw on them.

Left as they are: ``final.md`` and ``landscape_final_model`` still belong to an epoch's
identity (``epochs.current_components``), so changing either still makes a candidate, whose
regression gate then checks nothing for that change; and decisions recorded while S9 is off
carry no assessment id, so turning S9 back on shows every one of them as "changed since your
decision".
"""
from src.config import get_settings


def final_enabled() -> bool:
    """True unless ``LANDSCAPE_FINAL_ENABLED`` is false."""
    return get_settings().landscape_final_enabled is not False
