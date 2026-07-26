"""Staff editing of the organisational tags the display scoreboard reads.

The board in :mod:`judge.views.live_scoreboard` derives its badges and its
"in person" toggle from *organisation membership*, precisely so that who is in
the hall can be corrected on the day without a deploy. Doing that through the
Django admin means leaving the board, finding the organisation, and editing a
multi-select of every profile on the site -- awkward with a projector running
and a queue of people at the desk.

This module is the shortcut: one endpoint that toggles a competitor's
membership of the organisations *this event has configured as badges*, so the
change lands in the same place the admin site would have put it and shows up on
the next poll.

Deliberate constraints, all of which are load-bearing:

1. **Only the event's configured badge organisations may be touched.** The
   endpoint takes slugs, not organisation ids, and any slug outside
   ``badges``/``in_person_organization`` for this event is a 400. This is not a
   general-purpose membership editor that happens to live on the scoreboard.
2. **Only competitors in the event may be edited.** The target must be a live,
   non-disqualified participant of one of the event's contests. An arbitrary
   site user cannot be tagged through here.
3. **Permission is the same gate as the reveal** -- superuser, or able to edit
   every contest in the event (see :func:`~judge.views.live_scoreboard.can_reveal`).
   One privilege level for the whole board: if you are trusted to see behind
   the freeze, you are trusted to fix a badge, and not otherwise.

The changes are real organisation membership, so they are visible everywhere on
the site (organisation member lists, the short name beside a username during
contests) and are not scoped to this event. That is the point -- there is one
source of truth -- but it is worth knowing before wiring a button to it.

This lives in its own module rather than in ``live_scoreboard.py`` because that
file is the read path: it is polled every few seconds by a public page and has
no business importing anything that writes.
"""

import json
import logging

from django.db import transaction
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods

from judge.models import ContestParticipation, Organization, Profile
from judge.views.live_scoreboard import can_reveal, get_event_config, get_event_contests, resolve_badges

__all__ = ['live_scoreboard_tags']

logger = logging.getLogger('judge.scoreboard')


class TagEditError(Exception):
    """A bad request, carrying the status code to answer with."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def editable_badges(config):
    """The badge definitions this event allows staff to toggle.

    That is the configured ``badges``, in their configured order, plus the
    in-person organisation if it is not already one of them -- otherwise an
    event that uses ``in_person_organization`` without listing it as a badge
    could not have attendance corrected, which is the main thing this is for.

    Slugs that match no organisation are already dropped by ``resolve_badges``,
    so everything returned here is known to exist.
    """
    badges, in_person_key, _warnings = resolve_badges(config)
    definitions = list(badges)

    if in_person_key and not any(badge['key'] == in_person_key for badge in definitions):
        org = Organization.objects.filter(slug=in_person_key).only('slug', 'short_name', 'name').first()
        if org is not None:
            definitions.append({
                'key': org.slug,
                'label': org.short_name or org.name,
                'color': None,
            })

    return definitions, in_person_key


def _editable_organizations(keys):
    """Map the editable slugs to organisations, first match wins.

    ``Organization.slug`` is not unique at the database level, so this mirrors
    ``resolve_badges`` and keeps the first match rather than silently picking a
    different row to write to than the one the board reads from.
    """
    organizations = {}
    for org in Organization.objects.filter(slug__in=set(keys)).only('id', 'slug'):
        organizations.setdefault(org.slug, org)
    return organizations


def _load_body(request):
    if not request.body:
        raise TagEditError('Empty request body.')
    try:
        body = json.loads(request.body.decode('utf-8'))
    except (ValueError, UnicodeDecodeError):
        raise TagEditError('Request body must be JSON.')
    if not isinstance(body, dict):
        raise TagEditError('Request body must be a JSON object.')
    return body


def _string_list(body, field):
    value = body.get(field) or []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise TagEditError('"%s" must be a list of organisation slugs.' % field)
    for entry in value:
        if not isinstance(entry, str):
            raise TagEditError('"%s" must contain only organisation slugs.' % field)
    return list(value)


def _resolve_delta(body, allowed):
    """Work out which slugs to add and which to remove.

    The request is an explicit delta -- ``{"add": [...], "remove": [...]}`` --
    rather than a complete desired set, so a badge the request says nothing
    about is left alone instead of being written back over a change another
    organiser made in the meantime. The modal diffs its checkboxes against what
    they were when it opened and sends only what moved.

    The result is intersected with ``allowed``, so a slug outside this event's
    badges is rejected rather than quietly applied.
    """
    if 'add' not in body and 'remove' not in body:
        raise TagEditError('Nothing to do: send "add" and/or "remove".')

    add = set(_string_list(body, 'add'))
    remove = set(_string_list(body, 'remove'))

    unknown = (add | remove) - allowed
    if unknown:
        raise TagEditError('Not an editable badge for this event: %s' % ', '.join(sorted(unknown)))

    overlap = add & remove
    if overlap:
        raise TagEditError('Cannot both add and remove: %s' % ', '.join(sorted(overlap)))

    return add, remove


def _find_competitor(body, contests):
    """The profile being edited, which must be competing in this event.

    Restricting to entrants is what stops this being a way to edit the
    organisation membership of any user on the site.
    """
    username = body.get('username')
    if not isinstance(username, str) or not username.strip():
        raise TagEditError('A "username" is required.')
    username = username.strip()

    profile = (
        Profile.objects
        .filter(user__username=username)
        .prefetch_related('organizations')
        .first()
    )
    if profile is None:
        raise TagEditError('No such user: %s' % username, status=404)

    competes = ContestParticipation.objects.filter(
        contest__in=contests,
        user=profile,
        virtual=ContestParticipation.LIVE,
        is_disqualified=False,
    ).exists()
    if not competes:
        raise TagEditError('%s is not competing in this event.' % username, status=404)

    return profile


def _state(profile, badge_keys, in_person_key):
    """The row's tag state, shaped so the page can patch a row in place.

    ``badges`` comes back in the event's configured badge order, matching what
    ``build_contest_payload`` puts on each row, so the caller can assign it
    straight across without re-sorting.
    """
    member_of = {org.slug for org in profile.organizations.all()}
    return {
        'username': profile.user.username,
        'display_name': profile.display_name,
        'badges': [key for key in badge_keys if key in member_of],
        'in_person': bool(in_person_key) and in_person_key in member_of,
    }


@require_http_methods(['GET', 'POST'])
def live_scoreboard_tags(request, event):
    """Read or edit a competitor's organisational tags.

    ``GET`` describes what may be edited, so the page can render the modal
    without hard-coding this event's badges::

        {"badges": [{"key": "dev-onsite", "label": "On site", "color": null}]}

    ``POST`` applies a change to one competitor and returns their new state::

        {"username": "user01", "add": ["dev-onsite"], "remove": ["dev-beginner"]}

        -> {"username": "user01", "display_name": "Ada Lovelace",
            "badges": ["dev-onsite"], "in_person": true}

    Both verbs need the edit privilege. The page never asks whether it may
    edit -- the board's own payload already told it, and it only renders the
    button and the modal when the answer was yes.

    CSRF applies as normal, so the page must send ``X-CSRFToken``.
    """
    config = get_event_config(event)
    contests = get_event_contests(config)

    if not can_reveal(request.user, contests):
        # JSON rather than PermissionDenied's HTML error page: every other
        # failure here answers in JSON, and the caller is always a fetch().
        return JsonResponse({'error': 'You may not edit tags for this scoreboard.'}, status=403)

    badges, in_person_key = editable_badges(config)
    badge_keys = [badge['key'] for badge in badges]

    if request.method == 'GET':
        return JsonResponse({'badges': badges})

    try:
        body = _load_body(request)
        add, remove = _resolve_delta(body, set(badge_keys))
        profile = _find_competitor(body, contests)
    except TagEditError as e:
        return JsonResponse({'error': e.message}, status=e.status)

    organizations = _editable_organizations(add | remove)
    missing = (add | remove) - set(organizations)
    if missing:
        # resolve_badges already dropped unknown slugs, so this only fires if an
        # organisation was deleted between that query and this one.
        return JsonResponse({'error': 'Organisation no longer exists: %s' % ', '.join(sorted(missing))}, status=409)

    current = {org.slug for org in profile.organizations.all()}
    # Narrow to real changes so a double-click is a no-op and the audit log
    # only records things that actually moved.
    to_add = sorted(add - current)
    to_remove = sorted(remove & current)

    if to_add or to_remove:
        with transaction.atomic():
            if to_add:
                profile.organizations.add(*[organizations[slug] for slug in to_add])
            if to_remove:
                profile.organizations.remove(*[organizations[slug] for slug in to_remove])
        # Real membership changes made outside the admin site's own log, so
        # leave a trail of who did what.
        logger.info(
            'Scoreboard "%s": %s set tags on %s (+%s, -%s)',
            config['key'], request.user.username, profile.user.username,
            ','.join(to_add) or '-', ','.join(to_remove) or '-',
        )

    # The prefetch above is now stale; drop it so the response reports what is
    # actually in the database rather than what was there before the write.
    profile = (
        Profile.objects
        .filter(pk=profile.pk)
        .prefetch_related('organizations')
        .get()
    )

    return JsonResponse(_state(profile, badge_keys, in_person_key))
