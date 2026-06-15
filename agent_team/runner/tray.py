"""
agent_team.runner.tray — the system-tray icon for the local daemon.

Split into two layers so the tray is testable without a GUI:

  * ``build_menu_spec(...)`` returns the menu as PURE DATA — a list of
    ``(label, callback, checkable)`` — with no pystray/PIL dependency. Tests assert
    on this (labels present, Quit calls its callback, Pause toggles the flag).
  * ``build_icon(...)`` converts that spec into a real ``pystray.Icon`` (importing
    pystray/PIL lazily). It is exercised only at runtime; tests never call
    ``icon.run()`` (the headless CI box has no window server).
"""
from . import state

# Menu labels (zh-CN, brand = Tangent AI).
LABEL_OPEN = "打开面板"
LABEL_PAIR = "配对账户"
LABEL_WATCHED = "监控文件夹…"
LABEL_PAUSE = "暂停"
LABEL_QUIT = "退出"


def build_menu_spec(*, on_open, on_pair, on_watched, on_pause_toggle, on_quit):
    """The tray menu as pure data: ``[(label, callback, checkable), ...]``.

    Pair / Watched-folders are STUBS this phase (their callbacks just open a route
    or log "coming soon"); the real device-token pairing and the watchdog folder
    watcher are separate workstreams. Pause is a real toggle backed by the shared
    ``state`` flag the path-aware run endpoint checks."""
    return [
        (LABEL_OPEN, on_open, False),
        (LABEL_PAIR, on_pair, False),
        (LABEL_WATCHED, on_watched, False),
        (LABEL_PAUSE, on_pause_toggle, True),
        (LABEL_QUIT, on_quit, False),
    ]


def _make_image(size=64):
    """A simple branded glyph (Tangent blue disc with a 'T'); no external file."""
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, size - 4, size - 4), fill=(31, 94, 255, 255))  # #1f5eff
    d.text((size // 2 - 6, size // 2 - 12), "T", fill=(255, 255, 255, 255))
    return img


def build_icon(*, url, on_open, on_pair, on_watched, on_pause_toggle, on_quit,
               title="Tangent Agent"):
    """Build a configured ``pystray.Icon`` from the menu spec. Caller invokes
    ``.run()`` (blocking) on the main thread; this function never does."""
    import pystray

    spec = build_menu_spec(on_open=on_open, on_pair=on_pair, on_watched=on_watched,
                           on_pause_toggle=on_pause_toggle, on_quit=on_quit)

    def _plain(cb):
        return lambda icon, item: cb()

    items = []
    for label, cb, checkable in spec:
        if label == LABEL_QUIT:
            def _quit(icon, item, _cb=cb):
                try:
                    _cb()
                finally:
                    icon.stop()
            items.append(pystray.MenuItem(label, _quit))
        elif checkable:
            # The Pause item reflects (and toggles) the shared paused flag.
            def _toggle(icon, item, _cb=cb):
                _cb()
            items.append(pystray.MenuItem(
                label, _toggle, checked=lambda item: state.is_paused()))
        else:
            items.append(pystray.MenuItem(label, _plain(cb)))

    return pystray.Icon("tangent-agent", _make_image(), title, pystray.Menu(*items))
