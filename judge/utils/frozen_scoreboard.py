"""ICPC-style frozen scoreboard computation.

The scoring rules live here, separate from the view that queries for them, so
the freeze logic can be read in one place. Everything is passed in as plain
data:

* ``problems``     -- ordered list of problem descriptors
* ``participants`` -- list of competitor descriptors
* ``attempts``     -- flat iterable of :class:`Attempt`
* ``freeze_offset``-- seconds from contest start at which the board freezes

Times are expressed as *seconds from the start of the contest*, so the caller
does the timezone arithmetic and the rules below stay pure arithmetic.

The board is always scored ICPC-style (one point per solve, penalty minutes for
wrong attempts) regardless of the contest's configured ``format_name``. This is
a display board for an ICPC-style event; the contest's own ranking page still
uses whatever format it is configured with.
"""

from dataclasses import dataclass
from typing import Optional

from judge.models.submission import Submission

__all__ = [
    'SOLVED', 'FROZEN', 'JUDGING', 'FAILED', 'EMPTY',
    'CORRECT', 'PENDING', 'INCORRECT',
    'IGNORED_RESULTS', 'PENDING_RESULTS',
    'Attempt', 'build_scoreboard', 'classify_event', 'rank_rows',
]

# Cell states.
#
# FROZEN and JUDGING both mean "no result on screen", but for opposite reasons,
# and the board should not conflate them. FROZEN is the board withholding an
# answer it has; JUDGING is the judge not having produced one yet. Only the
# first is a spoiler, only the first is resolved by the reveal, and telling the
# hall the difference costs nothing -- a submission sitting in the queue at
# minute 12 is not evidence that the standings are provisional.
SOLVED = 'solved'     # accepted, and the accept happened before the freeze
FROZEN = 'frozen'     # something was submitted at or after the freeze
JUDGING = 'judging'   # something is still being judged, but all of it predates the freeze
FAILED = 'failed'     # attempted, no accept, nothing outstanding
EMPTY = 'empty'       # never attempted

# Event feed states. Coarser than the cell states, because the feed reports one
# submission rather than a competitor's whole history on a problem.
CORRECT = 'correct'
PENDING = 'pending'
INCORRECT = 'incorrect'

# Verdicts that never count for anything -- matches judge/contest_format/icpc.py,
# plus AB (aborted), which is likewise not the competitor's fault.
IGNORED_RESULTS = frozenset({'IE', 'CE', 'AB'})

# Verdicts meaning "not judged yet". These show as pending (blue) exactly like a
# post-freeze submission does, which is what you want on a live board. An
# unjudged submission has a null result, but a grading status can leak into the
# field, so the in-progress statuses count too ('D' is graded-but-not-scored).
PENDING_RESULTS = frozenset({None, '', 'D'}).union(Submission.IN_PROGRESS_GRADING_STATUS)


@dataclass
class Attempt:
    """One contest submission, flattened.

    :param participation: participation id
    :param problem: contest problem id
    :param time: seconds from contest start
    :param points: points awarded by the contest submission
    :param result: DMOJ verdict string, or None if not judged
    :param max_points: the contest problem's point value
    """

    participation: Optional[int]
    problem: int
    time: float
    points: float
    result: Optional[str]
    max_points: float

    @property
    def ignored(self):
        return self.result in IGNORED_RESULTS

    @property
    def pending(self):
        return self.result in PENDING_RESULTS

    @property
    def accepted(self):
        if self.pending:
            return False
        if self.result == 'AC':
            return True
        # Fall back to points for formats that award full marks without an 'AC'.
        return self.max_points > 0 and self.points >= self.max_points


def _resolve(attempts, upto=None):
    """Walk attempts in time order and find the first accept.

    :param upto: if given, only consider attempts strictly before this time.
    :return: (solved, solve_time, wrong_count, pending_count)
    """
    wrong = 0
    pending = 0
    for attempt in attempts:
        if attempt.ignored:
            continue
        if upto is not None and attempt.time >= upto:
            continue
        if attempt.accepted:
            return True, attempt.time, wrong, 0
        if attempt.pending:
            pending += 1
        else:
            wrong += 1
    return False, None, wrong, pending


def _cell(state, wrong=0, pending=0, time=None, penalty=0):
    return {'state': state, 'wrong': wrong, 'pending': pending, 'time': time, 'penalty': penalty}


def _cell_penalty(solve_time, wrong, penalty_minutes):
    """ICPC penalty for a solved cell: solve minute plus penalty per wrong try."""
    return int(solve_time // 60) + wrong * penalty_minutes


def _build_cell(attempts, freeze_offset, penalty_minutes, include_reveal):
    """Compute one competitor/problem cell from that pair's attempts."""
    attempts = sorted(attempts, key=lambda a: a.time)

    # What the public is allowed to see: everything strictly before the freeze.
    solved, solve_time, wrong, pending_before = _resolve(attempts, upto=freeze_offset)

    if solved:
        # Solved in the open. Anything submitted afterwards is irrelevant.
        return _cell(SOLVED, wrong=wrong, time=solve_time,
                     penalty=_cell_penalty(solve_time, wrong, penalty_minutes))

    frozen_count = sum(1 for a in attempts if not a.ignored and a.time >= freeze_offset)

    if frozen_count:
        # Something landed inside the freeze window, so there is an answer here
        # that the board is deliberately not showing.
        state = FROZEN
    elif pending_before:
        # Outstanding only because the judge has not caught up. Nothing is being
        # withheld, so do not dress it up as a frozen result.
        state = JUDGING
    elif wrong:
        state = FAILED
    else:
        state = EMPTY

    cell = _cell(state, wrong=wrong, pending=pending_before + frozen_count)

    if include_reveal and state == FROZEN:
        # The truth behind the freeze. Only ever serialised for admins.
        r_solved, r_time, r_wrong, _ = _resolve(attempts)
        cell['reveal'] = {
            'state': SOLVED if r_solved else FAILED,
            'wrong': r_wrong,
            'time': r_time,
            'penalty': _cell_penalty(r_time, r_wrong, penalty_minutes) if r_solved else 0,
        }

    return cell


def build_scoreboard(problems, participants, attempts, freeze_offset,
                     penalty_minutes=20, include_reveal=False):
    """Build a full frozen scoreboard.

    :param problems: ordered list of dicts with at least ``id``; ``label``,
        ``code`` and ``points`` are passed through to the output.
    :param participants: list of dicts with at least ``id`` and ``username``.
    :param attempts: iterable of :class:`Attempt`.
    :param freeze_offset: seconds from contest start at which the board freezes.
        Pass a very large number to disable freezing entirely.
    :param penalty_minutes: penalty added per wrong attempt on a solved problem.
    :param include_reveal: whether to include the hidden post-freeze results.
        **Only ever pass True for admins** -- this is the payload that would let
        a viewer read the frozen standings straight out of devtools.
    :return: dict with ``problems`` and ``rows``, rows sorted into rank order.
    """
    problem_order = [p['id'] for p in problems]
    known_problems = set(problem_order)

    # Bucket attempts by (participation, problem).
    buckets = {}
    for attempt in attempts:
        if attempt.problem in known_problems:
            buckets.setdefault((attempt.participation, attempt.problem), []).append(attempt)

    rows = []
    for competitor in participants:
        # An empty bucket falls out of _build_cell as an EMPTY cell on its own.
        cells = [
            _build_cell(buckets.get((competitor['id'], pid), ()), freeze_offset,
                        penalty_minutes, include_reveal)
            for pid in problem_order
        ]

        row = dict(competitor)
        row.update({
            'cells': cells,
            'solved': sum(1 for c in cells if c['state'] == SOLVED),
            'penalty': sum(c['penalty'] for c in cells if c['state'] == SOLVED),
        })
        rows.append(row)

    return {'problems': list(problems), 'rows': rank_rows(rows)}


def classify_event(attempt, freeze_offset):
    """How one submission should read in the live event feed.

    The feed is the only place the board narrates individual submissions, so
    the freeze has to be enforced here as well as on the grid: anything at or
    after ``freeze_offset`` reads as pending. Otherwise the sidebar would
    announce the very results the frozen cells are withholding.

    Unlike the grid, this has no admin escape hatch. The feed masks for
    everyone, reveal rights or not -- an admin driving the ceremony is looking
    at the same screen as the hall, and a sidebar quietly narrating the answers
    beside a frozen board spoils it for the person running it too. The grid's
    reveal is the one sanctioned way to see behind the freeze.

    Note that this is deliberately blind to *why* something is pending. A
    genuinely unjudged submission and a masked one are indistinguishable to a
    viewer, which is the point: the mask cannot be spotted by its shape.

    :return: ``(state, masked)``, where masked says the freeze changed the
        answer -- so the caller knows not to ship the real verdict alongside it.
    """
    if attempt.time >= freeze_offset:
        return PENDING, True
    if attempt.pending:
        return PENDING, False
    return (CORRECT if attempt.accepted else INCORRECT), False


def rank_rows(rows):
    """Sort rows into ICPC rank order and assign ranks, sharing ties.

    Sorted by solves descending, then penalty ascending, then username so the
    order is stable and reproducible between polls.
    """
    rows = sorted(rows, key=lambda r: (-r['solved'], r['penalty'], r['username']))

    last_key = None
    rank = 0
    for i, row in enumerate(rows):
        key = (row['solved'], row['penalty'])
        if key != last_key:
            rank = i + 1
            last_key = key
        row['rank'] = rank
    return rows
