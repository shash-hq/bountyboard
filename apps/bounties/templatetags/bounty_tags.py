from django import template
from django.utils import timezone

register = template.Library()


@register.filter(name="inr_format")
def inr_format(value):
    """Formats an integer or float with Indian Rupee grouping and symbol (e.g. ₹1,500)."""
    try:
        val = int(value)
        # Indian number formatting logic
        s = str(abs(val))
        if len(s) <= 3:
            res = s
        else:
            last3 = s[-3:]
            rest = s[:-3]
            chunks = []
            while len(rest) > 2:
                chunks.append(rest[-2:])
                rest = rest[:-2]
            if rest:
                chunks.append(rest)
            chunks.reverse()
            res = ",".join(chunks) + "," + last3
        prefix = "-" if val < 0 else ""
        return f"₹{prefix}{res}"
    except (ValueError, TypeError):
        return f"₹{value}"


@register.filter(name="time_left")
def time_left(deadline):
    """Computes a concise, humanized time remaining until deadline."""
    if not deadline:
        return ""
    now = timezone.now()
    if deadline <= now:
        return "Expired"
    diff = deadline - now
    days = diff.days
    hours = diff.seconds // 3600
    minutes = (diff.seconds % 3600) // 60

    if days > 1:
        return f"{days} days left"
    elif days == 1:
        return "1 day left"
    elif hours > 0:
        return f"{hours}h {minutes}m left"
    else:
        return f"{minutes}m left"
