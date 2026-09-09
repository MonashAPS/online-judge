import functools
from datetime import timezone

from django.template.defaultfilters import date, time
from django.templatetags.tz import localtime
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.utils.translation import gettext as _

from . import registry


def localtime_wrapper(func):
    @functools.wraps(func)
    def wrapper(datetime, *args, **kwargs):
        if getattr(datetime, 'convert_to_local_time', True):
            datetime = localtime(datetime)
        return func(datetime, *args, **kwargs)

    return wrapper


local_date = localtime_wrapper(date)
registry.filter(local_date)
registry.filter(localtime_wrapper(time))


@registry.function
def relative_time(time, **kwargs):
    # Format in the viewer's timezone, as the date filter does; the bare
    # defaultfilters.date keeps an aware datetime in its own tzinfo, which is
    # UTC straight from the database.
    abs_time = local_date(time, kwargs.get('format', _('N j, Y, g:i a')))
    return mark_safe(f'<span data-iso="{time.astimezone(timezone.utc).isoformat()}" class="time-with-rel"'
                     f' title="{escape(abs_time)}" data-format="{escape(kwargs.get("rel", _("{time}")))}">'
                     f'{escape(kwargs.get("abs", _("on {time}")).replace("{time}", abs_time))}</span>')
