"""Emoji and colours for Lab_nas messages, with plain-text fallbacks.

    emoji   only when the console can show them (Colab, Linux/macOS terminals,
            Windows Terminal, VS Code). Old Windows cmd windows get [OK], [X] ...
    colour  only when output goes to a terminal or Colab.
            NO_COLOR=1 turns colour off, FORCE_COLOR=1 turns it on.
    LAB_NAS_PLAIN=1 turns off both (handy for logs and scripts).

This module must not import the rest of Lab_nas (it is imported by it).
"""

import os
import sys

_CODES = {'bold': '1', 'dim': '2', 'red': '31', 'green': '32', 'yellow': '33',
          'blue': '34', 'magenta': '35', 'cyan': '36'}

# kind: (emoji, plain fallback, colour of the whole line or None)
_KINDS = {
    'ok':      ('✅', '[OK]',   'green'),
    'error':   ('❌', '[X]',    'red'),
    'warn':    ('⚠️', '[!]',    'yellow'),
    'info':    ('💬', '[i]',    None),
    'tip':     ('💡', '[tip]',  'dim'),
    'net':     ('🌐', '[net]',  None),
    'wait':    ('⏳', '[..]',   'cyan'),
    'key':     ('🔑', '[key]',  None),
    'save':    ('💾', '[save]', None),
    'install': ('🧩', '[pkg]',  'cyan'),
    'login':   ('🔓', '[in]',   'green'),
    'logout':  ('🔒', '[out]',  None),
    'stop':    ('🛑', '[stop]', 'yellow'),
    'down':    ('📥', '[down]', 'green'),
    'up':      ('📤', '[up]',   'green'),
    'zip':     ('📦', '[zip]',  'green'),
    'skip':    ('⏩', '[skip]', 'yellow'),
    'search':  ('🔍', '[?]',    'yellow'),
    'folder':  ('📂', '[dir]',  None),
    'dir':     ('📁', '[D]',    None),
    'file':    ('📄', '   ',    None),
    'log':     ('📝', '[log]',  None),
}


def _in_colab():
    return 'google.colab' in sys.modules


def _plain():
    return bool(os.environ.get('LAB_NAS_PLAIN'))


def use_color(stream=None):
    stream = stream or sys.stdout
    if _plain() or os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    if _in_colab():
        return True
    try:
        return stream.isatty()
    except Exception:
        return False


def use_emoji(stream=None):
    stream = stream or sys.stdout
    if _plain():
        return False
    if _in_colab():
        return True
    enc = getattr(stream, 'encoding', None) or 'ascii'
    try:
        '✅📥⚠️'.encode(enc)
    except (UnicodeEncodeError, LookupError):
        return False
    if os.name == 'nt':
        # The old console host can't draw emoji even in UTF-8; the newer ones can.
        return bool(os.environ.get('WT_SESSION') or os.environ.get('TERM_PROGRAM'))
    return True


_win_ansi_done = False


def paint(text, *styles, stream=None):
    """Wrap text in ANSI colour codes when colour is on for this stream."""
    global _win_ansi_done
    if not styles or not use_color(stream):
        return str(text)
    if os.name == 'nt' and not _win_ansi_done:
        os.system('')  # turns on ANSI handling in Windows 10+ consoles
        _win_ansi_done = True
    codes = ';'.join(_CODES[s] for s in styles if s in _CODES)
    return f'\x1b[{codes}m{text}\x1b[0m'


def hl(text, stream=None):
    """Highlight a value (a path, a name, an IP) inside a message."""
    return paint(text, 'bold', 'cyan', stream=stream)


def icon(kind, stream=None):
    emoji, plain, _ = _KINDS[kind]
    return emoji if use_emoji(stream) else plain


def say(kind, msg, file=None, indent=0):
    """Print one message with its icon, e.g. say('ok', 'Connected')."""
    stream = file or sys.stdout
    _, _, colour = _KINDS[kind]
    mark = icon(kind, stream)
    head = paint(mark, colour, stream=stream) if colour else mark
    if colour in ('red', 'yellow', 'dim'):
        msg = paint(msg, colour, stream=stream)
    line = f'{" " * indent}{head} {msg}' if mark.strip() else f'{" " * indent}{msg}'
    print(line, file=stream)
