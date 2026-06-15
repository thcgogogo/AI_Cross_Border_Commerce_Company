"""
Shared runtime flags for the local desktop daemon.

A LEAF module: it imports nothing from the app or any heavy dependency, so both
``app.py`` (the request hooks) and the tray can read/write it without an import
cycle. The daemon is single-process — waitress serves the Flask app in worker
threads inside the same process that owns the tray — so a module-level flag is
shared across the server threads and the tray menu.
"""
import threading

_paused = threading.Event()  # set == the daemon refuses NEW runs


def is_paused():
    return _paused.is_set()


def set_paused(value):
    """Set the paused flag; returns the new state."""
    if value:
        _paused.set()
    else:
        _paused.clear()
    return is_paused()


def toggle_paused():
    """Flip the paused flag; returns the new state."""
    return set_paused(not is_paused())
