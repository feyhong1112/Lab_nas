"""Command line for Lab_nas. Works the same in bash and Windows cmd.

    lab_nas start                          connect to NetBird
    lab_nas login --host [host address]    log in once, stay logged in (no time limit)
    lab_nas session | logout               show time left | log out now
    python -m Lab_nas path --fix           put the lab_nas command on PATH
    lab_nas info                           status at a glance (also: just `lab_nas`)
    lab_nas status | stop | log | where
    lab_nas ls /home          --host [host address]  
    lab_nas find "*.xlsx" /home
    lab_nas download /home/Drive/a.xlsx /home/Drive/Folder --dest .
    lab_nas upload report.xlsx data.csv --to /home/Drive

If `lab_nas` is not found, use `python -m Lab_nas ...` (Windows: `py -m Lab_nas ...`).

Environment variables (bash: export NAME=value | cmd: set NAME=value):
    NETBIRD_SETUP_KEY   setup key, only used when the saved identity can't connect
    LAB_NAS_HOST        NAS NetBird IP, so you can leave out --host
    SYNO_USER, SYNO_PASS  DSM login (asked for if missing)
    LAB_NAS_CONFIG_DIR  where the NetBird identity is kept

`lab_nas login` keeps only the DSM session ticket (never the password) in a
private file. NAS commands reuse it until you log out (or until the time
given with --minutes runs out), then it is logged out on the NAS and deleted. Giving --host/--user/--password on a command skips
the saved login for that command.
"""

import os
import sys
import argparse
import json
import time
import shlex
import shutil
import sysconfig

from .style import say, hl
from . import (NetBird, Synology, SynologyError, default_config_dir,
               __version__, __auther__, SOCKS_PORT, CONNECT_TIMEOUT)


def _netbird(a):
    kw = {'hostname': a.hostname, 'socks_port': a.socks_port,
          'timeout': a.timeout}
    if a.no_save:
        kw['config_dir'] = None
    elif a.config_dir:
        kw['config_dir'] = a.config_dir
    return NetBird(**kw)


# ---------------------------------------------------------- saved login

LOGIN_MINUTES = 0             # 0 = no time limit


def _session_file():
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
    else:
        base = os.environ.get('XDG_CACHE_HOME') or os.path.join(
            os.path.expanduser('~'), '.cache')
    return os.path.join(base, 'Lab_nas', 'session.json')


def _last_host_file():
    return os.path.join(os.path.dirname(_session_file()), 'last_host')


def _last_host():
    try:
        with open(_last_host_file()) as f:
            return f.read().strip() or None
    except OSError:
        return None


def _read_session():
    try:
        with open(_session_file()) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_session(data):
    path = _session_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    # 0o600: only you can read it (the ticket gives access to the NAS)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
    os.replace(tmp, path)


def _delete_session():
    try:
        os.remove(_session_file())
    except OSError:
        pass


def _nas_from_session(d):
    nas = Synology(d['host'], socks_port=d.get('socks_port', SOCKS_PORT),
                   use_proxy=d.get('use_proxy', 'auto'))
    nas.base, nas.login_method, nas.sid = d['base'], d['login_method'], d['sid']
    return nas


def _end_session(d, reason):
    """Log the saved ticket out on the NAS (best effort) and delete it."""
    try:
        _nas_from_session(d).logout(quiet=True)
    except Exception:
        pass
    _delete_session()
    if reason:
        say('warn', reason, file=sys.stderr)


def _saved_session():
    """The saved login if it is still valid, else None (expired ones are removed)."""
    d = _read_session()
    if not d:
        return None
    expires = d.get('expires', 0)
    if expires is not None and time.time() >= expires:   # None = no time limit
        _end_session(d, f'Saved login for {d.get("user")}@{d.get("host")} expired '
                        '- run "lab_nas login" again')
        return None
    return d


def _left(d):
    if d.get('expires') is None:
        return 'no time limit'
    m, sec = divmod(max(0, int(d['expires'] - time.time())), 60)
    return f'{m} min {sec} s'


# ------------------------------------------------------------ NAS access

def _nas(a):
    """Returns (nas, uses_saved_login)."""
    explicit = a.host or a.user or a.password
    d = None if explicit else _saved_session()
    env_host = os.environ.get('LAB_NAS_HOST')
    if d and (not env_host or env_host == d['host']):
        return _nas_from_session(d), True

    host = a.host or env_host
    if not host:
        raise SynologyError(None, 'Give the NAS address with --host, set LAB_NAS_HOST, '
                                  'or run "lab_nas login --host ..." once')
    use_proxy = False if a.no_proxy else 'auto'
    nas = Synology(host, socks_port=a.socks_port, use_proxy=use_proxy)
    nas.login(a.user, a.password, quiet=True)
    return nas, False


def _with_nas(fn):
    def run(a):
        nas, saved = _nas(a)
        try:
            return fn(nas, a)
        except SynologyError as e:
            if saved and e.code in (106, 107, 119):
                _delete_session()
                raise SynologyError(e.code, 'The NAS ended the saved login '
                                            '- run "lab_nas login" again') from None
            raise
        finally:
            if not saved:
                try:
                    nas.logout(quiet=True)
                except Exception:
                    pass
    return run


def cmd_login(a):
    old = _read_session()
    if old:
        _end_session(old, None)          # replace, don't leave the old ticket open
    host = (a.host or os.environ.get('LAB_NAS_HOST') or (old or {}).get('host')
            or _last_host())
    if not host:
        raise SynologyError(None, 'Give the NAS address with --host (or set LAB_NAS_HOST)')
    if a.minutes < 0:
        raise SynologyError(None, '--minutes must be 0 (no time limit) or more')
    use_proxy = False if a.no_proxy else 'auto'
    nas = Synology(host, socks_port=a.socks_port, use_proxy=use_proxy, keepalive=0)
    nas.login(a.user, a.password, quiet=True)
    d = {'host': host, 'base': nas.base, 'login_method': nas.login_method,
         'sid': nas.sid, 'user': nas._creds[0], 'use_proxy': nas.use_proxy,
         'socks_port': a.socks_port,
         'expires': time.time() + a.minutes * 60 if a.minutes else None}
    _write_session(d)
    with open(_last_host_file(), 'w') as f:   # not secret; so next time --host is optional
        f.write(host)
    length = f'for {a.minutes:g} min' if a.minutes else 'with no time limit'
    say('login', f'Logged in to {hl(nas.base)} as {hl(d["user"])} {length} '
                 '(password not saved)')


def cmd_logout(a):
    d = _read_session()
    if not d:
        say('info', 'Not logged in')
        return
    _end_session(d, None)
    say('logout', f'Logged out {hl(str(d.get("user")) + "@" + str(d.get("host")))}')


def cmd_session(a):
    d = _saved_session()
    if not d:
        say('warn', 'Not logged in (run "lab_nas login -H <NAS IP>")')
        return 1
    when = 'no time limit' if d.get('expires') is None else f'expires in {_left(d)}'
    say('login', f'Logged in as {hl(d["user"])} to {hl(d["base"])}, {hl(when)}')


@_with_nas
def cmd_ls(nas, a):
    nas.ls(a.path, pattern=a.pattern)


@_with_nas
def cmd_find(nas, a):
    nas.find(a.pattern, a.root, max_depth=a.max_depth)


@_with_nas
def cmd_download(nas, a):
    failed = 0
    for p in a.paths:
        try:
            nas.download(p, a.dest, a.overwrite, a.extract)
        except SynologyError as e:
            failed += 1
            say('error', f'Failed {p}: {e}')
    return 1 if failed else 0


@_with_nas
def cmd_upload(nas, a):
    failed = 0
    for p in a.files:
        try:
            nas.upload(p, a.to, a.overwrite, not a.no_create)
        except SynologyError as e:
            failed += 1
            say('error', f'Failed {p}: {e}')
    return 1 if failed else 0


def cmd_start(a):
    _netbird(a).start(setup_key=a.setup_key, use_system=not a.own, force=a.force)


def cmd_stop(a):
    _netbird(a).stop()


def cmd_status(a):
    _netbird(a).status()


def cmd_log(a):
    _netbird(a).log(a.lines)


def cmd_where(a):
    nb = _netbird(a)
    say('key',    f'Identity folder : {hl(nb.config_dir or "(not saved)")}')
    say('folder', f'Runtime folder  : {hl(nb.run_dir)}')
    say('log',    f'Log file        : {hl(nb.log_file)}')
    say('save',   f'Default folder  : {hl(default_config_dir())}')


# ----------------------------------------------------------------- PATH

_LAUNCHERS = ('lab_nas.exe', 'lab_nas') if os.name == 'nt' else ('lab_nas',)


def _script_dirs():
    """Folders where pip may have put the lab_nas launcher, most likely first."""
    dirs = [sysconfig.get_path('scripts')]
    try:
        scheme = sysconfig.get_preferred_scheme('user')        # Python 3.10+
    except AttributeError:
        scheme = 'nt_user' if os.name == 'nt' else 'posix_user'
    try:
        dirs.append(sysconfig.get_path('scripts', scheme))
    except KeyError:
        pass
    # If this copy of Lab_nas was installed with --user, its launcher is in the
    # user folder, so look there first
    try:
        import site
        here = os.path.normcase(os.path.abspath(__file__))
        user_site = os.path.normcase(os.path.abspath(site.getusersitepackages()))
        if here.startswith(user_site + os.sep):
            dirs.reverse()
    except Exception:
        pass
    seen, out = set(), []
    for d in dirs:
        k = os.path.normcase(os.path.abspath(d)) if d else None
        if k and k not in seen:
            seen.add(k)
            out.append(d)
    return out


def _launcher_dir():
    for d in _script_dirs():
        if any(os.path.isfile(os.path.join(d, n)) for n in _LAUNCHERS):
            return d
    return None


def _on_path(d):
    k = os.path.normcase(os.path.abspath(d))
    return any(os.path.normcase(os.path.abspath(os.path.expandvars(p))) == k
               for p in os.environ.get('PATH', '').split(os.pathsep) if p)


def _fix_path_windows(d):
    import winreg
    import ctypes
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment', 0,
                        winreg.KEY_READ | winreg.KEY_WRITE) as key:
        try:
            cur, kind = winreg.QueryValueEx(key, 'Path')
        except FileNotFoundError:
            cur, kind = '', winreg.REG_EXPAND_SZ
        parts = [p for p in cur.split(';') if p]
        want = os.path.normcase(os.path.abspath(d))
        if any(os.path.normcase(os.path.abspath(os.path.expandvars(p))) == want
               for p in parts):
            say('ok', f'Already in your user PATH: {hl(d)}')
        else:
            parts.append(d)
            # Written straight to the registry: `setx` would cut PATH at 1024 chars
            winreg.SetValueEx(key, 'Path', 0,
                              kind if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ)
                              else winreg.REG_EXPAND_SZ, ';'.join(parts))
            say('ok', f'Added to your user PATH: {hl(d)}')
    try:  # tell Explorer so new windows see it
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x001A, 0, 'Environment',
                                                 0x0002, 5000, None)
    except Exception:
        pass
    say('tip', 'Open a NEW cmd / PowerShell window, then run: lab_nas --version')


def _fix_path_linux(d):
    # Root with a writable /usr/local/bin (e.g. Colab): a link works right away
    usr = '/usr/local/bin'
    if os.access(usr, os.W_OK) and _on_path(usr):
        for name in ('lab_nas', 'lab-nas'):
            src, dst = os.path.join(d, name), os.path.join(usr, name)
            if os.path.isfile(src) and not os.path.exists(dst):
                os.symlink(src, dst)
        say('ok', f'Linked into {hl(usr)} - lab_nas works now')
        return
    line = f'export PATH={shlex.quote(d)}:"$PATH"   # added by Lab_nas'
    home = os.path.expanduser('~')
    files = [os.path.join(home, '.profile')]
    bashrc = os.path.join(home, '.bashrc')
    if os.path.exists(bashrc) or os.environ.get('SHELL', '').endswith('bash'):
        files.append(bashrc)
    for f in files:
        try:
            with open(f) as fh:
                if line in fh.read():
                    say('ok', f'Already in {hl(f)}')
                    continue
        except OSError:
            pass
        with open(f, 'a') as fh:
            fh.write('\n' + line + '\n')
        say('ok', f'Added to {hl(f)}')
    say('tip', 'Open a new terminal, or for this one run:')
    print(f'    export PATH={shlex.quote(d)}:"$PATH"')


def cmd_path(a):
    d = _launcher_dir()
    found = shutil.which('lab_nas')
    if found and (d is None or _on_path(d)):
        say('ok', f'lab_nas is on PATH ({hl(found)})')
        return 0
    if not d:
        say('error', 'The lab_nas launcher was not found in:')
        print(*('    ' + x for x in _script_dirs()), sep='\n')
        say('tip', 'Reinstall with:  python -m pip install --force-reinstall lab-me-nas')
        return 1
    say('warn', f'lab_nas is installed in {d}')
    print('     but that folder is not on PATH.')
    if not a.fix:
        say('tip', 'Run again with --fix to add it:  python -m Lab_nas path --fix')
        return 1
    if os.name == 'nt':
        _fix_path_windows(d)
    else:
        _fix_path_linux(d)
    return 0


def cmd_info(a):
    from .banner import show
    show(_saved_session)


class _CommandHelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Include input hints in the top-level command list only."""

    _inputs = {
        'start': '[-k KEY]',
        'log': '[-n COUNT]',
        'path': '[--fix]',
        'login': '[-H IP] [-u USERNAME] [-m MINUTES]',
        'ls': '[NAS_DIR]',
        'find': '<PATTERN> [NAS_DIR]',
        'download': '<NAS_PATH> [...] [-d LOCAL_DIR]',
        'upload': '<LOCAL_FILE> [...] -t <NAS_DIR>',
    }

    def __init__(self, prog):
        super().__init__(prog, max_help_position=58, width=110)

    def _format_action_invocation(self, action):
        label = super()._format_action_invocation(action)
        hint = self._inputs.get(action.dest)
        if hint and not action.option_strings:
            return f'{label} {hint}'
        return label


def build_parser():
    nb = argparse.ArgumentParser(add_help=False)
    g = nb.add_argument_group('NetBird options')
    g.add_argument('-N', '--hostname', metavar='NAME',
                   help='peer name, e.g. colab-lab (default: PC name)')
    g.add_argument('-c', '--config-dir', metavar='DIR',
                   help='saved identity folder, e.g. ./netbird; or set LAB_NAS_CONFIG_DIR')
    g.add_argument('--no-save', action='store_true',
                   help="don't keep the identity (a new peer every time; no value needed)")
    g.add_argument('-s', '--socks-port', type=int, default=SOCKS_PORT, metavar='PORT',
                   help=f'local SOCKS5 port, e.g. 1080 (default: {SOCKS_PORT})')
    g.add_argument('-T', '--timeout', type=int, default=CONNECT_TIMEOUT, metavar='SECONDS',
                   help=f'seconds before asking for the setup key (default: {CONNECT_TIMEOUT})')

    nas = argparse.ArgumentParser(add_help=False)
    g = nas.add_argument_group('NAS options')
    g.add_argument('-H', '--host', metavar='IP',
                   help='NAS NetBird IP, e.g.  [host address]; or set LAB_NAS_HOST')
    g.add_argument('-u', '--user', metavar='USERNAME',
                   help='DSM username, e.g.  [user]  ; or set SYNO_USER / enter when prompted')
    g.add_argument('-p', '--password', metavar='PASSWORD',
                   help='DSM password; prefer SYNO_PASS or the hidden prompt to avoid shell history')
    g.add_argument('-s', '--socks-port', type=int, default=SOCKS_PORT, metavar='PORT',
                   help=f'local SOCKS5 port (default: {SOCKS_PORT})')
    g.add_argument('--no-proxy', action='store_true',
                   help='connect directly using the NetBird app/service (no value needed)')

    p = argparse.ArgumentParser(
        prog='lab_nas', description='Synology NAS over NetBird, from bash or cmd.',
        formatter_class=_CommandHelpFormatter,
        epilog="""How to fill in arguments:
  <VALUE> is required; [VALUE] or [-flag VALUE] is optional; [...] means more inputs.
  Replace placeholders with your values; do not type the angle/square brackets.
  ls defaults to /; find defaults to searching / when NAS_DIR is omitted.
  Put options after the command: lab_nas login -H  [host address]   -u  [user]  
  -H IP and --host IP mean the same thing; replace IP with your NAS IP.
  Quote paths with spaces and patterns, e.g. "My Folder" and "*.xlsx".
  Switches such as -o / --overwrite take no value; omit to leave them off.
  Run lab_nas <command> --help for short flags, values and examples.

Command aliases: st = start, down = download, up = upload.
If lab_nas is not found, use python -m Lab_nas (Windows: py -m Lab_nas).""")
    p.add_argument('-V', '--version', action='version',
                   version=f'Lab_nas {__version__} [{__auther__}ðŸ¦¾]')
    sub = p.add_subparsers(dest='command', metavar='command')

    def command(name, help_text, examples, **kwargs):
        return sub.add_parser(
            name, help=help_text, description=help_text,
            formatter_class=argparse.RawDescriptionHelpFormatter,
            epilog='Examples (replace sample values with your own):\n  '
                   + '\n  '.join(examples), **kwargs)

    command('info', 'status at a glance', ['lab_nas info']).set_defaults(func=cmd_info)

    s = command('start', 'connect to NetBird', [
        'lab_nas start',
        'lab_nas st -N colab-lab -c "./netbird"',
        'lab_nas start -k YOUR_SETUP_KEY',
    ], aliases=['st'], parents=[nb])
    s.add_argument('-k', '-key', '--setup-key', metavar='KEY',
                   help='NetBird setup key; prefer NETBIRD_SETUP_KEY; '
                        'only used if the saved identity fails')
    s.add_argument('--own', action='store_true',
                   help="start Lab_nas's own NetBird even if a NetBird app is connected")
    s.add_argument('-f', '--force', action='store_true',
                   help='reconnect even if connected (no value needed)')
    s.set_defaults(func=cmd_start)

    command('stop', 'disconnect', ['lab_nas stop'], parents=[nb]).set_defaults(func=cmd_stop)
    command('status', 'show NetBird status', ['lab_nas status'], parents=[nb]).set_defaults(
        func=cmd_status)
    s = command('log', 'show the NetBird log', ['lab_nas log -n 100'], parents=[nb])
    s.add_argument('-n', '--lines', type=int, default=40, metavar='COUNT',
                   help='number of recent log lines, e.g. 100 (default: 40)')
    s.set_defaults(func=cmd_log)
    command('where', 'show where files are kept', ['lab_nas where'], parents=[nb]).set_defaults(
        func=cmd_where)

    s = command('path', 'check / fix that the lab_nas command is on PATH', [
        'python -m Lab_nas path', 'python -m Lab_nas path -f'])
    s.add_argument('-f', '--fix', action='store_true',
                   help='add the launcher folder to PATH (no value needed)')
    s.set_defaults(func=cmd_path)

    s = command('login', 'log in once and stay logged in', [
        'lab_nas login -H  [host address]  -u  [user]',
        'lab_nas login -m 60',
    ], parents=[nas])
    s.add_argument('-m', '--minutes', type=float, default=LOGIN_MINUTES, metavar='MINUTES',
                   help='log out after this many minutes, e.g. 60 '
                        '(default: 0 = no time limit)')
    s.set_defaults(func=cmd_login)
    command('logout', 'end the saved login now', ['lab_nas logout']).set_defaults(func=cmd_logout)
    command('session', 'show the saved login and time left', ['lab_nas session']).set_defaults(
        func=cmd_session)

    s = command('ls', 'list a NAS folder', [
        'lab_nas ls /home/Drive -P "*.pdf"',
        'lab_nas ls / -H  [host address]  -u  [user]  ',
    ], parents=[nas])
    s.add_argument('path', nargs='?', default='/', metavar='NAS_PATH',
                   help='NAS folder, e.g. /home/Drive (default: /)')
    s.add_argument('-P', '--pattern', metavar='PATTERN',
                   help='filename pattern; quote it, e.g. "*.pdf"')
    s.set_defaults(func=cmd_ls)

    s = command('find', 'search names on the NAS', [
        'lab_nas find "*.xlsx" /home/Drive -D 3',
    ], parents=[nas])
    s.add_argument('pattern', metavar='PATTERN',
                   help='filename pattern; quote it, e.g. "*.xlsx" or "report*"')
    s.add_argument('root', nargs='?', default='/', metavar='NAS_ROOT',
                   help='NAS folder to search, e.g. /home/Drive (default: /)')
    s.add_argument('-D', '--max-depth', type=int, default=5, metavar='DEPTH',
                   help='subfolder depth to search; 0 searches only the root (default: 5)')
    s.set_defaults(func=cmd_find)

    s = command('download', 'download files/folders', [
        'lab_nas down /home/Drive/report.xlsx -d .',
        'lab_nas download "/home/Drive/My Folder" -d ./save -x',
        'lab_nas down /home/Drive/a.csv /home/Drive/b.csv -d ./save -o',
    ], aliases=['down'], parents=[nas])
    s.add_argument('paths', nargs='+', metavar='NAS_PATH',
                   help='one or more NAS file/folder paths, separated by spaces; folders arrive as .zip')
    s.add_argument('-d', '--dest', default=None, metavar='LOCAL_DIR',
                   help='local destination, e.g. ./save (default: /content on Colab, current folder elsewhere)')
    s.add_argument('-o', '--overwrite', action='store_true',
                   help='replace existing local files (no value needed)')
    s.add_argument('-x', '--extract', action='store_true',
                   help='unzip downloaded folders (no value needed)')
    s.set_defaults(func=cmd_download)

    s = command('upload', 'upload local files', [
        'lab_nas up report.xlsx -t /home/Drive',
        'lab_nas upload "my report.xlsx" data.csv -t /home/Drive -o',
    ], aliases=['up'], parents=[nas])
    s.add_argument('files', nargs='+', metavar='LOCAL_FILE',
                   help='one or more local file paths, separated by spaces, e.g. report.xlsx data.csv')
    s.add_argument('-t', '--to', required=True, metavar='NAS_DIR',
                   help='required NAS destination folder, e.g. /home/Drive')
    s.add_argument('-o', '--overwrite', action='store_true',
                   help='replace existing NAS files (no value needed)')
    s.add_argument('--no-create', action='store_true',
                   help="don't create the NAS folder if it is missing (no value needed)")
    s.set_defaults(func=cmd_upload)
    return p


def main(argv=None):
    # Don't crash on consoles that can't print some characters (e.g. old cmd code pages)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:
            pass

    parser = build_parser()
    a = parser.parse_args(argv)
    if not a.command:                    # bare `lab_nas`: banner + how to get help
        cmd_info(a)
        say('net', 'Commands: ' + hl('start  login  ls  find  download  upload  session  logout  stop'), indent=2)
        say('tip', 'Help:     lab_nas -h   or   lab_nas <command> -h', indent=2)
        print()
        return 0
    try:
        return a.func(a) or 0
    except (SynologyError, RuntimeError) as e:
        say('error', f'Error: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(file=sys.stderr)
        say('stop', 'Cancelled', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
