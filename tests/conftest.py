"""Shared pytest configuration.

Phase 8 turns on a rate limiter (flask-limiter) by default, but the existing test
suite fires many auth/run requests from a single client and is NOT exercising rate
limits — leaving the limiter on would make those tests flaky with spurious 429s.

So we turn it OFF suite-wide HERE — but in a way that keeps the limiter fully
*initialized*. We let it construct in its on-by-default state (so flask-limiter's
``init_app`` runs to completion at import time, registering its ``before_request``
hook and a ``memory://`` storage backend *before* the app serves any request),
then immediately flip ``enabled = False``. The naive alternative — setting
``RATELIMIT_ENABLED=0`` before import — makes ``init_app`` early-return and skip
BOTH the storage and the hook, after which the dedicated Phase 8 rate-limit test
cannot re-arm the limiter (Flask forbids adding a ``before_request`` once the app
has handled a request). With this approach that test just sets ``enabled = True``.

``setdefault`` means an explicit ``RATELIMIT_ENABLED`` in the environment still wins.
"""
import os

os.environ.setdefault("RATELIMIT_ENABLED", "1")

import app  # noqa: E402 — imported here (after the env default) to disable the limiter

app.limiter.enabled = False
