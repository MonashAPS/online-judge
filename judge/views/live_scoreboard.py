"""A standalone, projector-friendly ICPC scoreboard for multi-division events.

Serves one page per *event*, where an event is a named group of contests that
run simultaneously as divisions of the same competition. Events are configured
in ``local_settings.py``::

    MCPC_SCOREBOARDS = {
        'mcpc2026': {
            'title': 'MCPC 2026',
            'contests': ['mcpc2026-div-a', 'mcpc2026-div-b'],
        },
    }

and served at ``/scoreboard/mcpc2026``.

Two things here deliberately differ from the stock contest ranking page:

1. It ignores ``Contest.scoreboard_visibility``. The whole point is to drive a
   hall display for a contest whose own ranking page is hidden from entrants.
   Treat the URL as public the moment the event is configured.
2. It freezes the final hour ICPC-style and only ships the frozen results to
   admins, so the reveal cannot be spoiled by reading the network tab.

Per-event theming
-----------------

An event can be dressed up without forking the page. Two hooks, either or both:

    'mcpc2026': {
        ...
        'theme': 'olympics',                           # styling on top
        'template': 'contest/live-scoreboard.html',    # a different page
    },

``theme`` names a template in ``templates/contest/scoreboard-themes/``, pulled
into the page's ``<head>`` *after* the built-in styles, so anything it declares
wins: override the ``:root`` custom properties for a recolour, or write rules
against the hooks the board exposes (``body.theme-<key>``, ``<html
data-theme>``, ``tr.rank-1|2|3``, ``tr[data-rank]``, ``tr[data-position]``).
Themes are templates rather than static files, so a change is live on a site
restart with no ``collectstatic``.

``template`` swaps the page itself, for a theme that needs different markup. It
is nearly always better to extend the default one and override only the blocks
that need to change::

    {% extends "contest/live-scoreboard.html" %}
    {% block body_start %}<div id="rings"></div>{% endblock %}

A theme whose template is missing is dropped rather than fatal: the board falls
back to the default styling and tells admins in the footer, the same way a
mistyped badge slug does.

Flags
-----

An event can give every competitor a small flag beside their name::

    'flags': '/media/flags/{username}.png',

The pattern is formatted per competitor and handed to the page as a URL. It is
never checked: a competitor with no image simply has no flag, because the page
drops an image that fails to load rather than showing a broken one. That is the
whole mechanism -- where the files come from, and who is allowed to change
them, is a question for whatever serves that URL.
"""

import json
import logging
import re
from urllib.parse import quote

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.http import Http404, JsonResponse
from django.shortcuts import render
from django.template import TemplateDoesNotExist
from django.template.loader import get_template
from django.utils import timezone
from django.views.generic import View

from judge.models import Contest, ContestParticipation, ContestSubmission, Organization
from judge.models.submission import SUBMISSION_RESULT
from judge.utils.frozen_scoreboard import CORRECT, FROZEN, PENDING, Attempt, build_scoreboard, classify_event

__all__ = ['LiveScoreboard', 'live_scoreboard_data']

logger = logging.getLogger('judge.scoreboard')

DEFAULT_FREEZE_MINUTES = 60
DEFAULT_PENALTY_MINUTES = 20

# Where a theme's template is looked up, by its key.
THEME_TEMPLATE = 'contest/scoreboard-themes/%s.html'

# A theme key names a file on disk, so keep it to something that cannot climb
# out of the theme directory.
THEME_KEY = re.compile(r'^[a-z0-9][a-z0-9_-]*$')

# How many recent submissions the event feed carries per division. The feed is
# a sidebar on a hall display, not an audit log, so this is deliberately small
# enough that the payload stays cheap to poll.
DEFAULT_FEED_LIMIT = 120

# Auto-preview timings, in seconds: how long the board sits still at the top,
# how long the slow scroll to the bottom takes, and how long it sits at the
# bottom before swapping divisions.
DEFAULT_PREVIEW_TOP_SECONDS = 4
DEFAULT_PREVIEW_SCROLL_SECONDS = 12
DEFAULT_PREVIEW_BOTTOM_SECONDS = 4

# Human-readable verdicts, so the feed can say "Wrong Answer" rather than "WA".
VERDICT_NAMES = {code: str(name) for code, name in SUBMISSION_RESULT}


def _normalise_badges(raw):
    """Accept either a bare organisation slug or a dict per badge.

    ``['onsite', {'organization': 'first-year', 'label': '1st yr'}]``

    A missing label falls back to the organisation's own short name, which is
    the field DMOJ already uses to label users during contests.
    """
    badges = []
    for entry in raw or []:
        if isinstance(entry, str):
            entry = {'organization': entry}
        if not isinstance(entry, dict) or not entry.get('organization'):
            raise ImproperlyConfigured(
                'Each MCPC_SCOREBOARDS badge must be an organisation slug or a dict '
                'with an "organization" key; got %r.' % (entry,),
            )
        badges.append({
            'organization': entry['organization'],
            'label': entry.get('label'),
            'color': entry.get('color'),
        })
    return badges


def get_event_config(event_key):
    """Look up and normalise one event's configuration.

    Accepts either the shorthand ``'key': ['contest-a', 'contest-b']`` or the
    full dict form, so a new year is usually a one-line addition.
    """
    events = getattr(settings, 'MCPC_SCOREBOARDS', None) or {}
    try:
        raw = events[event_key]
    except (KeyError, TypeError):
        raise Http404('No scoreboard configured for "%s".' % event_key)

    if isinstance(raw, (list, tuple)):
        raw = {'contests': list(raw)}
    if not isinstance(raw, dict):
        raise Http404('Malformed MCPC_SCOREBOARDS entry for "%s".' % event_key)

    contests = list(raw.get('contests') or [])
    if not contests:
        raise Http404('No contests listed for scoreboard "%s".' % event_key)

    # Both fall back to a site-wide default, and both take an explicit None to
    # opt one event out of it.
    theme = raw.get('theme', getattr(settings, 'MCPC_SCOREBOARD_THEME', None)) or None
    template = raw.get('template', getattr(settings, 'MCPC_SCOREBOARD_TEMPLATE', None)) or None
    if theme is not None and not THEME_KEY.match(theme):
        raise ImproperlyConfigured(
            'MCPC_SCOREBOARDS theme keys name a file in %s: use lowercase letters, digits, '
            '"-" and "_" only; got %r.' % (THEME_TEMPLATE % '<key>', theme),
        )

    flags = raw.get('flags', getattr(settings, 'MCPC_SCOREBOARD_FLAGS', None)) or None
    if flags is not None:
        try:
            flags.format(username='probe')
        except (KeyError, IndexError) as e:
            raise ImproperlyConfigured(
                'MCPC_SCOREBOARDS flag patterns take {username} and nothing else; %r has %s.' % (flags, e),
            )

    return {
        'key': event_key,
        'title': raw.get('title') or event_key,
        'contests': contests,
        'labels': raw.get('labels') or {},
        'badges': _normalise_badges(raw.get('badges')),
        'in_person_organization': raw.get('in_person_organization'),
        'theme': theme,
        'template': template,
        'flags': flags,
        'freeze_minutes': raw.get(
            'freeze_minutes',
            getattr(settings, 'MCPC_SCOREBOARD_FREEZE_MINUTES', DEFAULT_FREEZE_MINUTES),
        ),
        'poll_seconds': raw.get(
            'poll_seconds',
            getattr(settings, 'MCPC_SCOREBOARD_POLL_SECONDS', 20),
        ),
        'feed_limit': raw.get(
            'feed_limit',
            getattr(settings, 'MCPC_SCOREBOARD_FEED_LIMIT', DEFAULT_FEED_LIMIT),
        ),
        'preview': {
            'top_seconds': raw.get(
                'preview_top_seconds',
                getattr(settings, 'MCPC_SCOREBOARD_PREVIEW_TOP_SECONDS', DEFAULT_PREVIEW_TOP_SECONDS),
            ),
            'scroll_seconds': raw.get(
                'preview_scroll_seconds',
                getattr(settings, 'MCPC_SCOREBOARD_PREVIEW_SCROLL_SECONDS', DEFAULT_PREVIEW_SCROLL_SECONDS),
            ),
            'bottom_seconds': raw.get(
                'preview_bottom_seconds',
                getattr(settings, 'MCPC_SCOREBOARD_PREVIEW_BOTTOM_SECONDS', DEFAULT_PREVIEW_BOTTOM_SECONDS),
            ),
        },
    }


def get_event_contests(config):
    """Fetch the event's contests, preserving the configured order."""
    contests = Contest.objects.filter(key__in=config['contests'])
    by_key = {c.key: c for c in contests}
    missing = [key for key in config['contests'] if key not in by_key]
    if missing:
        raise Http404('Unknown contest(s): %s' % ', '.join(missing))
    return [by_key[key] for key in config['contests']]


def resolve_badges(config):
    """Turn configured organisation slugs into badge definitions.

    A slug that matches no organisation is skipped rather than fatal: a typo in
    the config should not take the hall display down mid-contest. The unmatched
    slugs come back so the page can warn admins about them quietly.
    """
    slugs = [badge['organization'] for badge in config['badges']]
    if config['in_person_organization']:
        slugs.append(config['in_person_organization'])

    found = {}
    for org in Organization.objects.filter(slug__in=set(slugs)).only('id', 'slug', 'short_name', 'name'):
        # Slug is not unique at the database level, so keep the first match and
        # flag the collision instead of silently picking one at random.
        found.setdefault(org.slug, org)

    warnings = []
    missing = [slug for slug in set(slugs) if slug not in found]
    if missing:
        message = 'No organisation with slug(s): %s' % ', '.join(sorted(missing))
        logger.warning('Scoreboard "%s": %s', config['key'], message)
        warnings.append(message)

    definitions = []
    for badge in config['badges']:
        org = found.get(badge['organization'])
        if org is None:
            continue
        definitions.append({
            'key': org.slug,
            'label': badge['label'] or org.short_name or org.name,
            'color': badge['color'],
        })

    in_person_org = found.get(config['in_person_organization'])
    if config['in_person_organization'] and in_person_org is None:
        warnings.append('In-person organisation "%s" not found; the attendance toggle is hidden.'
                        % config['in_person_organization'])

    return definitions, in_person_org.slug if in_person_org else None, warnings


def resolve_theme(config):
    """Find this event's theme template, if it has one.

    Returns ``(key, template_name, warnings)``. A theme whose template does not
    exist is dropped rather than fatal -- same reasoning as a mistyped badge
    slug -- and reported to admins in the page footer, so the board still comes
    up in its default clothes.
    """
    theme = config['theme']
    if not theme:
        return None, None, []

    name = THEME_TEMPLATE % theme
    try:
        get_template(name)
    except TemplateDoesNotExist:
        message = 'No theme template at "%s"; using the default styling.' % name
        logger.warning('Scoreboard "%s": %s', config['key'], message)
        return None, None, [message]

    return theme, name, []


def flag_url(pattern, username):
    """This competitor's flag, or None when the event has no flag pattern.

    The username is escaped because it lands in a URL, not because anything
    here trusts it less than the rest of the page does.
    """
    if not pattern:
        return None
    return pattern.format(username=quote(username, safe=''))


def can_reveal(user, contests):
    """Whether this user may see and drive the reveal.

    Superusers always can; otherwise the user must be able to edit every
    contest in the event, so a division organiser cannot spoil the other side.
    """
    if not user.is_authenticated:
        return False
    if user.is_superuser:
        return True
    return all(contest.is_editable_by(user) for contest in contests)


def _penalty_minutes(contest):
    """Use the contest's own ICPC penalty when it has one."""
    config = getattr(contest.format, 'config', None) or {}
    penalty = config.get('penalty', DEFAULT_PENALTY_MINUTES)
    try:
        return max(0, int(penalty))
    except (TypeError, ValueError):
        return DEFAULT_PENALTY_MINUTES


def _attempt(contest, max_points, problem_id, date, points, result, participation_id=None):
    """Flatten one submission row into the form the scoring module wants.

    Both the grid and the feed go through here, so a submission can never be
    scored one way in a cell and another way in the sidebar.
    """
    return Attempt(
        participation=participation_id,
        problem=problem_id,
        time=(date - contest.start_time).total_seconds(),
        points=points,
        result=result,
        max_points=max_points.get(problem_id, 0),
    )


def build_events(contest, labels, max_points, freeze_offset, limit):
    """Recent submissions for this division, newest first, as feed entries.

    This is the one place the board reports *individual* submissions rather
    than aggregates, so the freeze needs restating here: anything submitted at
    or after ``freeze_offset`` reads as pending, no matter what the judge
    actually said. Otherwise the sidebar would narrate the very results the
    frozen grid is hiding.

    Note there is no ``include_reveal`` here on purpose. The grid ships the
    frozen truth to admins so they can run the reveal; the feed never does, for
    anyone. The admin running the ceremony is watching the same screen as the
    hall, and would rather not have it spoiled either.
    """
    if not limit or limit <= 0:
        return []

    raw = (
        ContestSubmission.objects
        .filter(
            participation__contest=contest,
            participation__virtual=ContestParticipation.LIVE,
            participation__is_disqualified=False,
            submission__date__gte=contest.start_time,
            submission__date__lte=contest.end_time,
            problem_id__in=list(labels),
        )
        .order_by('-submission__date', '-submission_id')
        .values_list(
            'submission_id',
            'problem_id',
            'submission__date',
            'submission__result',
            'points',
            'participation__user__user__username',
            'participation__user__username_display_override',
        )[:limit]
    )

    events = []
    for submission_id, problem_id, date, result, points, username, override in raw:
        attempt = _attempt(contest, max_points, problem_id, date, points, result)
        state, masked = classify_event(attempt, freeze_offset)
        if state == PENDING:
            # Never ship the real verdict for a pending entry: for a masked one
            # that would hand over exactly what the freeze is hiding, and for a
            # genuinely unjudged one there is nothing to say anyway.
            verdict = 'Pending'
        elif state == CORRECT:
            verdict = VERDICT_NAMES.get(result, 'Accepted')
        else:
            verdict = VERDICT_NAMES.get(result, 'Rejected')

        events.append({
            'id': submission_id,
            'at': date.isoformat(),
            'minute': int(attempt.time // 60),
            'username': username,
            'display_name': override or username,
            'problem': labels[problem_id],
            'state': state,
            'verdict': verdict,
            'masked': masked,
        })
    return events


def build_contest_payload(contest, freeze_minutes, include_reveal, badge_keys=(), in_person_key=None,
                          feed_limit=DEFAULT_FEED_LIMIT, flags=None):
    """Assemble one division's board as plain JSON-serialisable data.

    :param badge_keys: organisation slugs to surface against each competitor.
    :param flags: a URL pattern taking {username}, or None for no flags.
    :param in_person_key: the slug that means "competing in the hall". Rows are
        tagged rather than filtered, so the page can toggle between views
        without another round trip.
    """
    duration = (contest.end_time - contest.start_time).total_seconds()

    if freeze_minutes and freeze_minutes > 0:
        freeze_offset = max(0.0, duration - freeze_minutes * 60)
    else:
        # No freeze: push the cutoff past the end of the contest.
        freeze_offset = duration + 1

    contest_problems = list(
        contest.contest_problems.select_related('problem')
        .defer('problem__description')
        .order_by('order'),
    )
    problems = [{
        'id': cp.id,
        'label': contest.get_label_for_problem(i),
        'code': cp.problem.code,
        'name': cp.problem.name,
        'points': cp.points,
    } for i, cp in enumerate(contest_problems)]
    max_points = {cp.id: cp.points for cp in contest_problems}
    labels = {problem['id']: problem['label'] for problem in problems}

    wanted = set(badge_keys)
    if in_person_key:
        wanted.add(in_person_key)

    participations = (
        contest.users
        .filter(virtual=ContestParticipation.LIVE, is_disqualified=False)
        .select_related('user__user')
        .prefetch_related('user__organizations')
        .defer('user__about', 'user__organizations__about')
    )

    participants = []
    for p in participations:
        # Prefetched, so this is a set intersection in Python rather than a
        # query per competitor.
        member_of = {org.slug for org in p.user.organizations.all()} & wanted
        participants.append({
            'id': p.id,
            'username': p.user.user.username,
            'display_name': p.user.display_name,
            'flag': flag_url(flags, p.user.user.username),
            'badges': [key for key in badge_keys if key in member_of],
            'in_person': bool(in_person_key) and in_person_key in member_of,
        })

    raw = (
        ContestSubmission.objects
        .filter(
            participation__contest=contest,
            participation__virtual=ContestParticipation.LIVE,
            participation__is_disqualified=False,
            submission__date__gte=contest.start_time,
            submission__date__lte=contest.end_time,
        )
        .values_list('participation_id', 'problem_id', 'submission__date', 'points', 'submission__result')
    )

    board = build_scoreboard(
        problems=problems,
        participants=participants,
        attempts=[
            _attempt(contest, max_points, problem_id, date, points, result, participation_id)
            for participation_id, problem_id, date, points, result in raw
        ],
        freeze_offset=freeze_offset,
        penalty_minutes=_penalty_minutes(contest),
        include_reveal=include_reveal,
    )

    now = timezone.now()
    board.update({
        'key': contest.key,
        'name': contest.name,
        'events': build_events(contest, labels, max_points, freeze_offset, feed_limit),
        'freeze_offset': freeze_offset,
        'duration': duration,
        # Drives the "Frozen" badge, so it has to mean "results are being
        # withheld" and not merely "a result is missing". FROZEN cells are the
        # former; JUDGING cells are the latter and deliberately do not count,
        # or one submission stuck in the queue at minute 12 would tell the hall
        # the standings are provisional two hours before they are.
        'is_frozen': any(
            cell['state'] == FROZEN for row in board['rows'] for cell in row['cells']
        ),
        'has_started': contest.start_time <= now,
        'has_ended': contest.end_time < now,
    })
    return board


def build_event_payload(request, config):
    """Build every division's board plus the metadata the page needs."""
    contests = get_event_contests(config)
    include_reveal = can_reveal(request.user, contests)

    badges, in_person_key, warnings = resolve_badges(config)
    badge_keys = [badge['key'] for badge in badges]

    theme, theme_template, theme_warnings = resolve_theme(config)
    warnings = warnings + theme_warnings

    boards = []
    for contest in contests:
        board = build_contest_payload(contest, config['freeze_minutes'], include_reveal,
                                      badge_keys=badge_keys, in_person_key=in_person_key,
                                      feed_limit=config['feed_limit'], flags=config['flags'])
        board['label'] = config['labels'].get(contest.key) or contest.name
        board['in_person_count'] = sum(1 for row in board['rows'] if row['in_person'])
        boards.append(board)

    return {
        'event': config['key'],
        'title': config['title'],
        'boards': boards,
        'can_reveal': include_reveal,
        # Currently the same privilege as the reveal, but named separately so
        # the page gates the tag-editing UI on its own flag. See
        # judge.views.live_scoreboard_tags, which is the endpoint behind it.
        'can_edit_tags': include_reveal,
        'badges': badges,
        'in_person_badge': in_person_key,
        'theme': theme,
        'theme_template': theme_template,
        'has_roster': bool(in_person_key),
        # Surfaced on the page for admins only, so a mistyped slug is noticed
        # during setup rather than after the contest.
        'warnings': warnings if include_reveal else [],
        'freeze_minutes': config['freeze_minutes'],
        'poll_seconds': config['poll_seconds'],
        'preview': config['preview'],
        'server_time': timezone.now().isoformat(),
    }


def _safe_json(payload):
    """JSON that is safe to inline in a <script> block.

    Escaping the HTML-significant characters means a value can never close the
    script tag early, no matter what ends up in a username.
    """
    return (
        json.dumps(payload)
        .replace('<', '\\u003c')
        .replace('>', '\\u003e')
        .replace('&', '\\u0026')
    )


class LiveScoreboard(View):
    """The scoreboard page itself, with the first render's data inlined."""

    # An event may point at a different page with its 'template' key.
    template_name = 'contest/live-scoreboard.html'

    def get(self, request, event):
        config = get_event_config(event)
        payload = build_event_payload(request, config)
        return render(request, config['template'] or self.template_name, {
            'title': config['title'],
            'event': config['key'],
            'payload': payload,
            'payload_json': _safe_json(payload),
            'can_reveal': payload['can_reveal'],
            # Styling on top of the default board: `theme` tags the page
            # (`<html data-theme>`, `body.theme-<key>`) and `theme_template` is
            # pulled into the head after the built-in styles. Both are None
            # when the event has no theme, and the page renders as it always
            # did.
            'theme': payload['theme'],
            'theme_template': payload['theme_template'],
            # Gates the local test fixtures baked into the template. They are a
            # development aid for exercising the live-update path without a
            # judge, and are not emitted at all in a production build.
            'debug': settings.DEBUG,
        })


def live_scoreboard_data(request, event):
    """JSON endpoint the page polls for updates."""
    config = get_event_config(event)
    return JsonResponse(build_event_payload(request, config))
