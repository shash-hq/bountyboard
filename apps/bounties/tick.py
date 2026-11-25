"""POST /internal/tick: the scheduler's single entry point.

A GitHub Actions cron calls it every ~10 minutes (the free web tier has no workers or cron, and
sleeps when idle, so the call also wakes it). Each call, in order: closes overdue bounties, then
drains the email outbox. Both are idempotent and safe to run concurrently, so an overlapping or
repeated call does no harm. Running queries also keeps the free database from pausing.

Authentication is a shared secret in ``X-Tick-Secret``, compared in constant time. With no secret
configured the endpoint does not exist (404): it fails closed.
"""
import hmac
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import Http404, JsonResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_ratelimit.decorators import ratelimit

from . import outbox, services

logger = logging.getLogger(__name__)

TICK_BUDGET_SECONDS = 20  # leave headroom under the caller's timeout on a 0.1-CPU instance


@login_not_required
@csrf_exempt
@never_cache
@require_POST
@ratelimit(key="ip", rate="30/m", method="POST", block=True)
def tick_view(request):
    secret = settings.TICK_SECRET
    if not secret:
        raise Http404("not found")
    supplied = request.headers.get("X-Tick-Secret", "")
    if not hmac.compare_digest(supplied.encode(), secret.encode()):
        return JsonResponse({"error": "forbidden"}, status=403)

    result = {}
    try:
        result["expired"] = services.sweep_expired()
    except Exception:  # noqa: BLE001 - one half failing must not skip the other
        logger.exception("tick: expiry sweep failed")
        result["expired_error"] = True
    try:
        result["outbox"] = outbox.drain(max_seconds=TICK_BUDGET_SECONDS)
    except Exception:  # noqa: BLE001
        logger.exception("tick: outbox drain failed")
        result["outbox_error"] = True
    return JsonResponse(result, status=500 if ("expired_error" in result or "outbox_error" in result) else 200)
