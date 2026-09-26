"""Command line for Lab_nas. Works the same in bash and Windows cmd.

    lab_nas start                          connect to NetBird
    lab_nas status | stop | log | where
    lab_nas ls /home          --host 100.83.14.114
    lab_nas find "*.xlsx" /home
    lab_nas download /home/Drive/a.xlsx /home/Drive/Folder --dest .
    lab_nas upload report.xlsx data.csv --to /home/Drive

If `lab_nas` is not found, use `python -m Lab_nas ...` (Windows: `py -m Lab_nas ...`).

Environment variables (bash: export NAME=value | cmd: set NAME=value):
    NETBIRD_SETUP_KEY   setup key, only used when the saved identity can't connect
    LAB_NAS_HOST        NAS NetBird IP, so you can leave out --host
    SYNO_USER, SYNO_PASS  DSM login (asked for if missing)
    LAB_NAS_CONFIG_DIR  where the NetBird identity is kept
"""

import os
import sys
import argparse

from . import (NetBird, Synology, SynologyError, default_config_dir,
               __version__, SOCKS_PORT, CONNECT_TIMEOUT)


def _netbird(a):
    kw = {'hostname': a.hostname, 'socks_port': a.socks_port,
          'timeout': a.timeout}
    if a.no_save:
        kw['config_dir'] = None
    elif a.config_dir:
        kw['config_dir'] = a.config_dir
    return NetBird(**kw)


def _nas(a):
    host = a.host or os.environ.get('LAB_NAS_HOST')
    if not host:
        raise SynologyError(None, 'Give the NAS address with --host, '
                                  'or set LAB_NAS_HOST')
    use_proxy = False if a.no_proxy else 'auto'
    nas = Synology(host, socks_port=a.socks_port, use_proxy=use_proxy)
    nas.login(a.user, a.password, quiet=True)
    return nas


def _with_nas(fn):
    def run(a):
        nas = _nas(a)
        try:
            return fn(nas, a)
        finally:
            try:
                nas.logout(quiet=True)
            except Exception:
                pass
    return run


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
            print(f'Failed {p}: {e}')
    return 1 if failed else 0


@_with_nas
def cmd_upload(nas, a):
    failed = 0
    for p in a.files:
        try:
            nas.upload(p, a.to, a.overwrite, not a.no_create)
        except SynologyError as e:
            failed += 1
            print(f'Failed {p}: {e}')
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
    print(f'Identity folder : {nb.config_dir or "(not saved)"}')
    print(f'Runtime folder  : {nb.run_dir}')
    print(f'Log file        : {nb.log_file}')
    print(f'Default folder  : {default_config_dir()}')


def build_parser():
    nb = argparse.ArgumentParser(add_help=False)
    g = nb.add_argument_group('NetBird options')
    g.add_argument('--hostname', help='peer name shown in NetBird (default: PC name)')
    g.add_argument('--config-dir', help='folder for the saved identity (config.json)')
    g.add_argument('--no-save', action='store_true',
                   help="don't keep the identity (a new peer every time)")
    g.add_argument('--socks-port', type=int, default=SOCKS_PORT,
                   help=f'local SOCKS5 port (default {SOCKS_PORT})')
    g.add_argument('--timeout', type=int, default=CONNECT_TIMEOUT,
                   help=f'seconds before asking for the setup key (default {CONNECT_TIMEOUT})')

    nas = argparse.ArgumentParser(add_help=False)
    g = nas.add_argument_group('NAS options')
    g.add_argument('--host', help='NAS NetBird IP (or set LAB_NAS_HOST)')
    g.add_argument('--user', help='DSM username (or set SYNO_USER)')
    g.add_argument('--password', help='DSM password (better: set SYNO_PASS, or be asked)')
    g.add_argument('--socks-port', type=int, default=SOCKS_PORT, help=argparse.SUPPRESS)
    g.add_argument('--no-proxy', action='store_true',
                   help='connect to the NAS directly (NetBird app/service installed)')

    p = argparse.ArgumentParser(
        prog='lab_nas', description='Synology NAS over NetBird, from bash or cmd.',
        epilog='Run "lab_nas <command> -h" for the options of a command.')
    p.add_argument('-V', '--version', action='version', version=f'Lab_nas {__version__}')
    sub = p.add_subparsers(dest='command', metavar='command')
    sub.required = True

    s = sub.add_parser('start', parents=[nb], help='connect to NetBird')
    s.add_argument('--setup-key', help='setup key (better: set NETBIRD_SETUP_KEY); '
                                       'only used if the saved identity fails')
    s.add_argument('--own', action='store_true',
                   help="start Lab_nas's own NetBird even if a NetBird app is connected")
    s.add_argument('--force', action='store_true', help='reconnect even if connected')
    s.set_defaults(func=cmd_start)

    sub.add_parser('stop', parents=[nb], help='disconnect').set_defaults(func=cmd_stop)
    sub.add_parser('status', parents=[nb], help='show NetBird status').set_defaults(
        func=cmd_status)
    s = sub.add_parser('log', parents=[nb], help='show the NetBird log')
    s.add_argument('-n', '--lines', type=int, default=40)
    s.set_defaults(func=cmd_log)
    sub.add_parser('where', parents=[nb], help='show where files are kept').set_defaults(
        func=cmd_where)

    s = sub.add_parser('ls', parents=[nas], help='list a NAS folder')
    s.add_argument('path', nargs='?', default='/')
    s.add_argument('--pattern', help='only names matching, e.g. "*.pdf"')
    s.set_defaults(func=cmd_ls)

    s = sub.add_parser('find', parents=[nas], help='search names on the NAS')
    s.add_argument('pattern', help='e.g. "*.xlsx" (quote it in bash)')
    s.add_argument('root', nargs='?', default='/')
    s.add_argument('--max-depth', type=int, default=5)
    s.set_defaults(func=cmd_find)

    s = sub.add_parser('download', parents=[nas], help='download files/folders')
    s.add_argument('paths', nargs='+', help='NAS paths (folders arrive as .zip)')
    s.add_argument('--dest', default=None, help='local folder (default: current folder)')
    s.add_argument('--overwrite', action='store_true')
    s.add_argument('--extract', action='store_true', help='unzip downloaded folders')
    s.set_defaults(func=cmd_download)

    s = sub.add_parser('upload', parents=[nas], help='upload local files')
    s.add_argument('files', nargs='+')
    s.add_argument('--to', required=True, help='NAS folder, e.g. /home/Drive')
    s.add_argument('--overwrite', action='store_true')
    s.add_argument('--no-create', action='store_true',
                   help="don't create the NAS folder if it is missing")
    s.set_defaults(func=cmd_upload)
    return p


def main(argv=None):
    # Don't crash on consoles that can't print some characters (e.g. old cmd code pages)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except Exception:
            pass

    a = build_parser().parse_args(argv)
    try:
        return a.func(a) or 0
    except (SynologyError, RuntimeError) as e:
        print(f'Error: {e}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('\nCancelled', file=sys.stderr)
        return 130


if __name__ == '__main__':
    sys.exit(main())
