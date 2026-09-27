"""Neofetch-style banner for `lab_nas` / `lab_nas info`.

The drawing is a NAS with live status lights:
    first light  = NetBird   (green connected, red not)
    second light = NAS login (green logged in, yellow not)
Colours are off when NO_COLOR is set or output is not a terminal (Colab is
treated as a terminal because the notebook shows colours). FORCE_COLOR=1 forces them.
"""

import os
import re
import sys
import time
import shutil
import platform

from . import NetBird, __version__, _in_colab, _port_open, SOCKS_PORT

# ---------------------------------------------------------------- colours

_CODES = {'reset': '0', 'bold': '1', 'dim': '2', 'red': '31', 'green': '32',
          'yellow': '33', 'blue': '34', 'magenta': '35', 'cyan': '36', 'white': '37'}
_ANSI = re.compile(r'\x1b\[[0-9;]*m')


def _want_color():
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('FORCE_COLOR'):
        return True
    if _in_colab():
        return True
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


class _Paint:
    def __init__(self, on):
        self.on = on
        if on and os.name == 'nt':
            os.system('')  # turns on ANSI colour handling in Windows 10+ consoles

    def __call__(self, text, *styles):
        if not self.on or not styles:
            return text
        return f"\x1b[{';'.join(_CODES[s] for s in styles)}m{text}\x1b[0m"


def _width(s):
    return len(_ANSI.sub('', s))


# ------------------------------------------------------------------ art

# {a} = NetBird light, {b} = login light. Every line has the same width.
_ART_UNICODE = [
    '╭──────────────────╮',
    '│ ┌──┐┌──┐┌──┐┌──┐ │',
    '│ │▓▓││▓▓││▓▓││▓▓│ │',
    '│ │▓▓││▓▓││▓▓││▓▓│ │',
    '│ └──┘└──┘└──┘└──┘ │',
    '│  {a} {b}     Lab_nas │',
    '╰────────┬─────────╯',
    '   ◉─────┼─────◉    ',
    '         ◉          ',
]
_ART_ASCII = [
    '+------------------+',
    '| +--++--++--++--+ |',
    '| |##||##||##||##| |',
    '| |##||##||##||##| |',
    '| +--++--++--++--+ |',
    '|  {a} {b}     Lab_nas |',
    '+--------+---------+',
    '   o-----+-----o    ',
    '         o          ',
]


def _can_draw_unicode():
    enc = getattr(sys.stdout, 'encoding', None) or 'ascii'
    try:
        ''.join(_ART_UNICODE).encode(enc)
        '●✓✗'.encode(enc)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def _art(p, uni, nb_on, login_on):
    lines = _ART_UNICODE if uni else _ART_ASCII
    dot = '●' if uni else '*'
    a = p(dot, 'green', 'bold') if nb_on else p(dot, 'red', 'bold')
    b = p(dot, 'green', 'bold') if login_on else p(dot, 'yellow', 'bold')
    out = []
    for i, line in enumerate(lines):
        if '{a}' in line:
            left, rest = line.split('{a}')
            mid, right = rest.split('{b}')
            right_label = right.replace('Lab_nas', p('Lab_nas', 'bold', 'white'))
            out.append(p(left, 'cyan') + a + p(mid, 'cyan') + b + p('', 'cyan')
                       + right_label.replace('│', p('│', 'cyan')).replace('|', p('|', 'cyan')))
        elif i >= len(lines) - 2:
            out.append(p(line, 'blue', 'bold'))       # the mesh network
        elif '▓' in line or '#' in line:
            out.append(p(line[:2], 'cyan') + p(line[2:-2], 'dim')
                       + p(line[-2:], 'cyan'))         # drive bays
        else:
            out.append(p(line, 'cyan'))
    return out


# ------------------------------------------------------------------ info

def _os_name():
    if _in_colab():
        return f'Google Colab ({platform.machine()})'
    if os.name == 'nt':
        return f'Windows {platform.release()} ({platform.machine()})'
    try:
        with open('/etc/os-release') as f:
            for line in f:
                if line.startswith('PRETTY_NAME='):
                    return line.split('=', 1)[1].strip().strip('"') + f' ({platform.machine()})'
    except OSError:
        pass
    return f'{platform.system()} {platform.release()} ({platform.machine()})'


def _netbird_state():
    """(connected, text) without downloading or waiting on anything slow."""
    nb = NetBird()
    if not nb.running():
        return False, 'not running  (lab_nas start)'
    info = nb.info()
    if not info:
        return False, 'daemon running, no status'
    if not info.get('management', {}).get('connected'):
        return False, 'not connected'
    ip = (info.get('netbirdIp') or '').split('/')[0]
    fqdn = info.get('fqdn') or ''
    peers = info.get('peers') or {}
    text = f'connected  {ip}'
    if fqdn:
        text += f'  ({fqdn})'
    if peers:
        text += f"\n{peers.get('connected', 0)}/{peers.get('total', 0)} peers active"
    return True, text


def _login_state(session_reader):
    d = session_reader() if session_reader else None
    if not d:
        return False, 'not logged in  (lab_nas login)'
    left = max(0, int(d['expires'] - time.time()))
    return True, f"{d.get('user')} @ {d.get('host')}  ({left // 60} min left)"


def show(session_reader=None):
    p = _Paint(_want_color())
    uni = _can_draw_unicode()
    ok, bad = ('✓', '✗') if uni else ('ok', 'x')

    nb_on, nb_text = _netbird_state()
    login_on, login_text = _login_state(session_reader)
    socks = _port_open(SOCKS_PORT)
    exe = shutil.which('lab_nas')

    title = f'Lab_nas {__version__}'
    rows = [
        p(title, 'bold', 'cyan'),
        p('─' * len(title) if uni else '-' * len(title), 'cyan'),
    ]

    def row(key, value, good=None):
        mark = '' if good is None else ' ' + (p(ok, 'green') if good else p(bad, 'red'))
        lines = value.split('\n')
        rows.append(p(f'{key}:', 'bold', 'cyan') + ' ' + lines[0] + mark)
        for extra in lines[1:]:
            rows.append(' ' * (len(key) + 2) + p(extra, 'dim'))

    row('OS', _os_name())
    row('Python', platform.python_version())
    row('NetBird', nb_text, nb_on)
    row('SOCKS5', f'127.0.0.1:{SOCKS_PORT} ' + ('listening' if socks else 'off'), socks)
    row('NAS', login_text, login_on)
    row('Command', exe or 'not on PATH  (python -m Lab_nas path --fix)', bool(exe))

    blocks = '███' if uni else '###'
    rows.append('')
    rows.append(''.join(p(blocks, c) for c in
                        ('red', 'green', 'yellow', 'blue', 'magenta', 'cyan', 'white')))

    art = _art(p, uni, nb_on, login_on)
    art_w = max(_width(a) for a in art)
    print()
    for i in range(max(len(art), len(rows))):
        left = art[i] if i < len(art) else ''
        right = rows[i] if i < len(rows) else ''
        print('  ' + left + ' ' * (art_w - _width(left)) + '   ' + right)
    print()
