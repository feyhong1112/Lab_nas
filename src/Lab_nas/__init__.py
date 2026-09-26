"""
Lab_nas - Access a Synology NAS through a NetBird mesh network,
from Google Colab, Linux or Windows.

Install:

    pip install Lab_nas

Python:

    from Lab_nas import NetBird, Synology

    NetBird().start()                      # connect to NetBird
    nas = Synology('100.83.14.114')
    nas.login()                            # SYNO_USER / SYNO_PASS
    nas.ls('/')                            # list shared folders
    nas.find('*.xlsx', '/home/Drive')      # search by name
    nas.download('/home/Drive/file.xlsx')  # download a file
    nas.download('/home/Drive/MyFolder')   # a folder arrives as MyFolder.zip
    nas.upload('file.xlsx', '/home/Drive') # upload a local file

Terminal (bash) / cmd:

    lab_nas start
    lab_nas ls / --host 100.83.14.114
    (or: python -m Lab_nas ...)

Secrets are looked up in this order: environment variable, Colab secret
(🔑 sidebar, Notebook access on), then you are asked to type it.
    NETBIRD_SETUP_KEY   reusable NetBird setup key
    SYNO_USER           DSM username
    SYNO_PASS           DSM password

NetBird start-up order:
    1. already connected (this library's daemon, or a NetBird app/service
       already running on the machine)          -> nothing to do
    2. saved identity (config.json)             -> reconnect, no setup key
    3. NETBIRD_SETUP_KEY (argument/env/secret)  -> used without asking
    4. still failing, or no answer in 60 s      -> you are asked for the key

Where the identity (config.json) is saved:
    Colab    /content/drive/MyDrive/netbird
    Linux    ~/.config/Lab_nas/netbird   (or $XDG_CONFIG_HOME/Lab_nas/netbird)
    Windows  <Documents>\\Lab_nas\\netbird
    Override with NetBird(config_dir=...), --config-dir, or LAB_NAS_CONFIG_DIR.
"""

import os
import re
import sys
import json
import time
import shutil
import signal
import socket
import fnmatch
import tarfile
import tempfile
import subprocess
import urllib.request
from contextlib import contextmanager

__version__ = '0.2.1'
__all__ = ['NetBird', 'Synology', 'SynologyError', 'default_config_dir']

IS_WIN = os.name == 'nt'
SOCKS_PORT = 1080
DAEMON_PORT = 41799           # our own daemon; never clashes with a system NetBird
CONNECT_TIMEOUT = 60          # seconds to wait for `netbird up`
_UNSET = object()


# ----------------------------------------------------------------- helpers

def _in_colab():
    try:
        import google.colab  # noqa: F401
        return True
    except ImportError:
        return False


def _colab_kernel():
    """True only inside the Colab notebook kernel itself.

    Commands run with `!` (e.g. `!lab_nas start`) are on a Colab machine but
    outside the kernel, where drive.mount() and userdata.get() cannot work.
    """
    if not _in_colab():
        return False
    try:
        from IPython import get_ipython
        ip = get_ipython()
        return ip is not None and getattr(ip, 'kernel', None) is not None
    except Exception:
        return False


def _find_secret(name):
    """Environment variable first, then Colab secret. Never prompts."""
    if not name:
        return None
    val = os.environ.get(name)
    if val:
        return val.strip()
    if _colab_kernel():
        try:
            from google.colab import userdata
            val = userdata.get(name)
            if val:
                return val.strip()
        except Exception:
            pass
    return None


def _ask(prompt, hidden=False):
    """Ask the user; returns '' when nobody can answer (no stdin)."""
    try:
        if hidden:
            from getpass import getpass
            return getpass(prompt).strip()
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return ''


def _get_secret(name, prompt=None, hidden=False):
    """Environment variable, Colab secret, or ask the user."""
    val = _find_secret(name)
    if val:
        return val
    if prompt is None:
        raise RuntimeError(f'Secret {name} not found')
    return _ask(prompt, hidden)


def _documents_dir():
    """The user's Documents folder (follows OneDrive/redirection on Windows)."""
    if IS_WIN:
        try:
            import ctypes
            buf = ctypes.create_unicode_buffer(260)
            # CSIDL_PERSONAL = 5 -> "Documents"
            if ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buf) == 0 \
                    and buf.value:
                return buf.value
        except Exception:
            pass
    return os.path.join(os.path.expanduser('~'), 'Documents')


def default_config_dir():
    """Folder where the NetBird identity (config.json) is kept."""
    env = os.environ.get('LAB_NAS_CONFIG_DIR')
    if env:
        return os.path.expanduser(env)
    if _in_colab():
        return '/content/drive/MyDrive/netbird'
    if IS_WIN:
        return os.path.join(_documents_dir(), 'Lab_nas', 'netbird')
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.join(
        os.path.expanduser('~'), '.config')
    return os.path.join(base, 'Lab_nas', 'netbird')


def _default_dest():
    return '/content' if _in_colab() else os.getcwd()


def _port_open(port, host='127.0.0.1'):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0


def _human(n):
    n = float(n or 0)
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024:
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} PB'


def _ensure_pysocks():
    try:
        import socks  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, '-m', 'pip', '-q', 'install', 'requests[socks]'],
                       check=True)


def _ensure_toolbelt():
    """requests-toolbelt lets us stream an upload (low memory) with a progress bar."""
    try:
        import requests_toolbelt  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, '-m', 'pip', '-q', 'install', 'requests-toolbelt'],
                       check=True)


def _ensure_alive():
    """alive-progress draws the animated live bar for uploads/downloads."""
    try:
        import alive_progress  # noqa: F401
    except ImportError:
        subprocess.run([sys.executable, '-m', 'pip', '-q', 'install', 'alive-progress'],
                       check=True)


@contextmanager
def _progress(total, title, show=True):
    """Live transfer bar. Yields update(delta_bytes), called as bytes move.

    Uses alive-progress for an animated bar with a live rate; if that library
    can't be loaded it falls back to a plain carriage-return status line so
    transfers still work everywhere.
    """
    if not show:
        yield lambda _delta: None
        return

    try:
        _ensure_alive()
        from alive_progress import alive_bar
    except Exception:
        # ---- fallback: simple \r line
        state = {'done': 0, 'start': time.time(), 'last': 0.0}

        def update(delta):
            state['done'] += delta
            now = time.time()
            if now - state['last'] > 0.5:
                done = state['done']
                speed = done / max(now - state['start'], 1e-6)
                pct = f' {done * 100 / total:5.1f}%' if total else ''
                print(f'\r{title}: {_human(done)}'
                      f'{" / " + _human(total) if total else ""}{pct}'
                      f'  {_human(speed)}/s   ', end='')
                state['last'] = now

        try:
            yield update
        finally:
            print()
        return

    # ---- alive-progress animated bar (unknown total -> unbounded spinner)
    with alive_bar(total or None, title=title, unit='B', scale='IEC',
                   precision=1, force_tty=True) as bar:
        yield lambda delta: bar(delta)


# ----------------------------------------------------------------- NetBird

class NetBird:
    """Runs NetBird in userspace (netstack) mode with a local SOCKS5 proxy.

    Works in Colab, on Linux and on Windows. No tun device / admin driver is
    needed. The peer identity (config.json) is saved in `config_dir`, so the
    machine reconnects as the same peer (same name, same NetBird IP) without
    needing the setup key again.

    config_dir   where config.json is kept (default: see default_config_dir());
                 None = don't keep it (a new peer every time)
    hostname     peer name shown in NetBird (default 'colab' or the PC name)
    timeout      seconds to wait for a connection before asking for the key
    """

    def __init__(self, config_dir=_UNSET, hostname=None, socks_port=SOCKS_PORT,
                 setup_key_secret='NETBIRD_SETUP_KEY', timeout=CONNECT_TIMEOUT,
                 daemon_port=DAEMON_PORT, drive_dir=_UNSET):
        # drive_dir is the old (v0.1) name of config_dir; still accepted
        if config_dir is _UNSET:
            config_dir = drive_dir
        self.config_dir = (default_config_dir() if config_dir is _UNSET
                           else (os.path.expanduser(config_dir) if config_dir else None))
        self.colab = _in_colab()
        self.hostname = hostname or ('colab' if self.colab else
                                     socket.gethostname().split('.')[0].lower())
        self.socks_port = socks_port
        self.setup_key_secret = setup_key_secret
        self.timeout = timeout
        self.daemon_port = daemon_port
        self.daemon_addr = f'tcp://127.0.0.1:{daemon_port}'
        self.exe = None
        self.mode = None          # 'netstack' (ours) or 'system' (existing NetBird)
        self._flags = None

        # Where the running daemon keeps its files. In Colab the Drive folder
        # is only used for saving/restoring config.json (Drive is slow + FUSE).
        if self.colab:
            self.run_dir = '/content/.lab_nas'
        elif self.config_dir:
            self.run_dir = self.config_dir
        else:
            self.run_dir = os.path.join(tempfile.gettempdir(), 'Lab_nas_netbird')

    def __repr__(self):
        return (f'<NetBird {self.hostname!r} config_dir={self.config_dir!r} '
                f'socks={self.socks_port}>')

    # ---- paths

    @property
    def run_cfg(self):
        return os.path.join(self.run_dir, 'config.json')

    @property
    def saved_cfg(self):
        return os.path.join(self.config_dir, 'config.json') if self.config_dir else None

    @property
    def log_file(self):
        return os.path.join(self.run_dir, 'netbird.log')

    @property
    def pid_file(self):
        return os.path.join(self.run_dir, 'netbird.pid')

    @property
    def drive_cfg(self):  # v0.1 name
        return self.saved_cfg

    # ---- setup

    def _prepare_dirs(self):
        if (self.colab and self.config_dir
                and self.config_dir.startswith('/content/drive')
                and not os.path.isdir('/content/drive/MyDrive')):
            if not _colab_kernel():
                raise RuntimeError(
                    'Google Drive is not mounted, and it can only be mounted from a '
                    'notebook cell. Run this in a cell first:\n'
                    '    from google.colab import drive; drive.mount("/content/drive")\n'
                    'or keep the identity elsewhere: --config-dir /content/netbird '
                    '(lost when the runtime resets)')
            from google.colab import drive
            drive.mount('/content/drive')
        for d in filter(None, (self.config_dir, self.run_dir)):
            os.makedirs(d, exist_ok=True)
            if not IS_WIN:
                try:
                    os.chmod(d, 0o700)   # config.json holds a private key
                except OSError:
                    pass

    @staticmethod
    def _latest_tag():
        """Return the latest NetBird release tag (e.g. 'v0.79.0').

        Tries the GitHub API first; if that is unavailable or rate-limited,
        falls back to reading the redirect target of the /releases/latest page.
        """
        api = 'https://api.github.com/repos/netbirdio/netbird/releases/latest'
        try:
            with urllib.request.urlopen(api, timeout=30) as r:
                tag = json.load(r).get('tag_name')
                if tag:
                    return tag
        except Exception:
            pass
        req = urllib.request.Request(
            'https://github.com/netbirdio/netbird/releases/latest', method='HEAD')
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.url.rstrip('/').rsplit('/', 1)[-1]

    @staticmethod
    def _platform():
        machine = (os.environ.get('PROCESSOR_ARCHITECTURE', '') if IS_WIN
                   else os.uname().machine).lower()
        arch = 'arm64' if machine in ('aarch64', 'arm64') else 'amd64'
        system = ('windows' if IS_WIN else
                  'darwin' if sys.platform == 'darwin' else 'linux')
        return system, arch

    @staticmethod
    def _bin_dir():
        if IS_WIN:
            base = os.environ.get('LOCALAPPDATA') or os.path.expanduser('~')
            return os.path.join(base, 'Lab_nas', 'bin')
        if os.access('/usr/local/bin', os.W_OK):
            return '/usr/local/bin'
        return os.path.join(os.path.expanduser('~'), '.local', 'bin')

    def install(self):
        """Find netbird, or download it from the official GitHub releases."""
        name = 'netbird.exe' if IS_WIN else 'netbird'
        own = os.path.join(self._bin_dir(), name)
        self.exe = shutil.which('netbird') or (own if os.path.isfile(own) else None)
        if self.exe:
            return self.exe

        print('Installing NetBird...')
        tag = self._latest_tag()
        ver = tag.lstrip('v')
        system, arch = self._platform()
        url = (f'https://github.com/netbirdio/netbird/releases/download/'
               f'{tag}/netbird_{ver}_{system}_{arch}.tar.gz')
        tgz = os.path.join(tempfile.gettempdir(), 'netbird.tar.gz')
        urllib.request.urlretrieve(url, tgz)
        os.makedirs(os.path.dirname(own), exist_ok=True)
        with tarfile.open(tgz) as tar:
            member = tar.getmember(name)
            with tar.extractfile(member) as src, open(own, 'wb') as dst:
                shutil.copyfileobj(src, dst)
        if not IS_WIN:
            os.chmod(own, 0o755)
        os.remove(tgz)
        self.exe = own
        print(f'NetBird {ver} installed to {own}')
        return own

    def _cli(self, *args, timeout=15, daemon=True):
        cmd = [self.exe or self.install(), *args]
        if daemon:
            cmd += ['--daemon-addr', self.daemon_addr]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _up_flags(self):
        """Which optional `up` flags this netbird version knows."""
        if self._flags is None:
            try:
                h = self._cli('up', '--help', daemon=False).stdout
            except Exception:
                h = ''
            self._flags = {f for f in ('--no-browser', '--setup-key-file') if f in h}
        return self._flags

    # ---- state

    def running(self):
        """Is this library's NetBird daemon up?"""
        return _port_open(self.daemon_port)

    def info(self, system=False):
        """Parsed `netbird status --json` (ours, or the system NetBird's)."""
        try:
            if system:
                # Skip the 10 s grpc wait if no system daemon is listening
                if IS_WIN:
                    if not _port_open(41731):
                        return {}
                elif not os.path.exists('/var/run/netbird.sock'):
                    return {}
                r = self._cli('status', '--json', daemon=False)
            else:
                if not self.running():
                    return {}
                r = self._cli('status', '--json')
            return json.loads(r.stdout) if r.returncode == 0 else {}
        except Exception:
            return {}

    def connected(self, system=False):
        return bool(self.info(system).get('management', {}).get('connected'))

    def ip(self, system=False):
        return (self.info(system).get('netbirdIp') or '').split('/')[0] or None

    def status(self):
        if not self.exe:
            self.install()
        sysinfo = self.info(system=True)
        if sysinfo:
            print('System NetBird (app/service on this machine):',
                  'connected' if sysinfo.get('management', {}).get('connected')
                  else sysinfo.get('daemonStatus', 'not connected'),
                  sysinfo.get('netbirdIp', ''))
        if self.running():
            r = self._cli('status')
            print(r.stdout.strip() or r.stderr.strip())
        else:
            print('Lab_nas NetBird daemon: not running')
        print(f'SOCKS5 proxy on {self.socks_port}:',
              'listening' if _port_open(self.socks_port) else 'NOT listening')
        print(f'Identity saved in: {self.config_dir or "(not saved)"}')

    def log(self, lines=40):
        try:
            with open(self.log_file, errors='replace') as f:
                print(''.join(f.readlines()[-lines:]))
        except FileNotFoundError:
            print('No log file yet')

    # ---- identity

    @property
    def marker_file(self):
        """Written only after a successful connect, so a config.json that was
        created but never registered isn't mistaken for a working identity."""
        return os.path.join(self.config_dir, '.registered') if self.config_dir else None

    def _restore_identity(self):
        """Put the saved config.json in place. True if it is a usable identity."""
        saved = self.saved_cfg
        if not saved or not os.path.exists(saved):
            return False
        if self.colab:
            # In Colab, config.json is copied to Drive only after a successful
            # connect (this is also true for files saved by v0.1).
            shutil.copy(saved, self.run_cfg)
        elif not os.path.exists(self.marker_file):
            return False
        print(f'Found saved NetBird identity ({saved})')
        return True

    def _save_identity(self):
        saved = self.saved_cfg
        if not saved:
            return
        if os.path.exists(self.run_cfg) \
                and os.path.abspath(saved) != os.path.abspath(self.run_cfg):
            shutil.copy(self.run_cfg, saved)
        if os.path.exists(saved) and not IS_WIN:
            try:
                os.chmod(saved, 0o600)
            except OSError:
                pass
        with open(self.marker_file, 'w') as f:
            f.write(time.strftime('%Y-%m-%d %H:%M:%S'))

    # ---- daemon

    def _read_pid(self):
        try:
            with open(self.pid_file) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _is_netbird(pid):
        try:
            if IS_WIN:
                out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}', '/NH'],
                                     capture_output=True, text=True).stdout
                return 'netbird' in out.lower()
            if os.path.exists(f'/proc/{pid}/cmdline'):
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    return b'netbird' in f.read()
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def _launch(self):
        env = dict(os.environ,
                   NB_USE_NETSTACK_MODE='true',
                   NB_SOCKS5_LISTENER_PORT=str(self.socks_port),
                   NB_STATE_DIR=os.path.join(self.run_dir, 'state'))
        kw = {}
        if IS_WIN:
            kw['creationflags'] = (getattr(subprocess, 'DETACHED_PROCESS', 0x8) |
                                   getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0x200))
        else:
            kw['start_new_session'] = True
        log = open(self.log_file, 'w')
        p = subprocess.Popen([self.exe, 'service', 'run', '--config', self.run_cfg,
                              '--daemon-addr', self.daemon_addr, '--log-file', 'console'],
                             env=env, stdout=log, stderr=log,
                             stdin=subprocess.DEVNULL, **kw)
        log.close()
        with open(self.pid_file, 'w') as f:
            f.write(str(p.pid))

        for _ in range(20):
            if self.running():
                return
            if p.poll() is not None:
                break
            time.sleep(1)
        self.log()
        hint = (' On Windows, try running cmd "as Administrator".' if IS_WIN else '')
        raise RuntimeError(f'NetBird daemon did not start (log above).{hint}')

    def stop(self, quiet=False):
        """Stop this library's daemon (a system NetBird is never touched)."""
        if self.running():
            try:
                self._cli('down', timeout=15)
            except Exception:
                pass
        pid = self._read_pid()
        if pid and self._is_netbird(pid):
            if IS_WIN:
                subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'],
                               capture_output=True)
            else:
                try:
                    os.kill(pid, signal.SIGTERM)
                    for _ in range(10):
                        time.sleep(0.5)
                        os.kill(pid, 0)
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        try:
            os.remove(self.pid_file)
        except OSError:
            pass
        if not quiet:
            print('NetBird stopped')

    # ---- connecting

    def _up(self, key=None, timeout=None):
        """Run `netbird up` once. Returns (ok, reason)."""
        timeout = timeout or self.timeout
        flags = self._up_flags()
        args = ['up', '--hostname', self.hostname]
        if '--no-browser' in flags:
            args.append('--no-browser')     # never pop up an SSO browser page
        key_file = None
        if key:
            if '--setup-key-file' in flags:  # keeps the key out of the process list
                fd, key_file = tempfile.mkstemp(dir=self.run_dir, prefix='.key')
                with os.fdopen(fd, 'w') as f:
                    f.write(key)
                args += ['--setup-key-file', key_file]
            else:
                args += ['--setup-key', key]
        try:
            r = self._cli(*args, timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                self._cli('down', timeout=15)    # cancel the pending login
            except Exception:
                pass
            return False, f'no answer within {timeout} s'
        finally:
            if key_file:
                try:
                    os.remove(key_file)
                except OSError:
                    pass
        if r.returncode == 0:
            return True, ''
        errs = [l for l in (r.stderr + r.stdout).splitlines() if 'Error' in l]
        reason = errs[-1] if errs else (r.stderr.strip().splitlines() or ['failed'])[-1]
        m = re.search(r'desc = (.*)', reason)
        return False, (m.group(1) if m else reason)[:200]

    def _connect(self, setup_key, have_identity):
        # 1) saved identity, no key needed
        if have_identity:
            print(f'Connecting with the saved identity (up to {self.timeout} s)...')
            ok, why = self._up()
            if ok:
                return
            print(f'  Saved identity did not connect: {why}')

        # 2) a key we already have: argument / env var / Colab secret
        key = setup_key or _find_secret(self.setup_key_secret)
        if key:
            src = 'argument' if setup_key else self.setup_key_secret
            print(f'Connecting with the setup key from {src} (up to {self.timeout} s)...')
            ok, why = self._up(key)
            if ok:
                return
            print(f'  Setup key did not work: {why}')

        # 3) ask the user
        for attempt in range(3):
            key = _ask('NetBird setup key (Enter to cancel): ', hidden=True)
            if not key:
                break
            print(f'Connecting (up to {self.timeout} s)...')
            ok, why = self._up(key)
            if ok:
                return
            print(f'  Failed: {why}')
        raise RuntimeError('Could not connect to NetBird. Check the setup key '
                           '(NETBIRD_SETUP_KEY) and look at the log (lab_nas log, or NetBird().log())')

    def start(self, setup_key=None, timeout=None, use_system=True, force=False):
        """Connect to NetBird. Safe to run again at any time.

        setup_key   only used if the saved identity can't connect
        timeout     seconds to wait for each connection attempt (default 60)
        use_system  if a NetBird app/service on this machine is already
                    connected, use it instead of starting our own
        force       restart our daemon even if it is already connected
        """
        if timeout:
            self.timeout = timeout
        self._prepare_dirs()
        self.install()

        # Already connected by the NetBird app/service (e.g. set up in a terminal)
        if use_system and not force and self.connected(system=True):
            self.mode = 'system'
            print(f'NetBird is already connected on this machine '
                  f'(IP {self.ip(system=True)}). No setup key needed; '
                  f'the NAS is reached directly, without the SOCKS5 proxy.')
            return self

        # Our own daemon is already up and connected
        if not force and self.running() and self.connected() \
                and _port_open(self.socks_port):
            self.mode = 'netstack'
            print(f'NetBird already connected as "{self.hostname}" '
                  f'(IP {self.ip()}), SOCKS5 proxy on {self.socks_port}')
            return self

        self.stop(quiet=True)
        have_identity = self._restore_identity()
        self._launch()
        try:
            self._connect(setup_key, have_identity)
        except BaseException:
            self.stop(quiet=True)       # don't leave a half-started daemon behind
            raise

        time.sleep(2)
        self._save_identity()

        for _ in range(15):
            if _port_open(self.socks_port):
                break
            time.sleep(1)
        else:
            self.log()
            raise RuntimeError(f'SOCKS5 proxy not listening on {self.socks_port}')

        self.mode = 'netstack'
        print(f'NetBird connected as "{self.hostname}" (IP {self.ip()}), '
              f'SOCKS5 proxy on {self.socks_port}')
        if self.saved_cfg:
            print(f'Identity saved to {self.saved_cfg}')
        return self


# ---------------------------------------------------------------- Synology

class SynologyError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(f'Error {code}: {message}' if code else message)


class Synology:
    """Synology File Station client that talks to the NAS through NetBird."""

    ERRORS = {
        100: 'Unknown error', 101: 'Invalid parameter', 102: 'API does not exist',
        103: 'Method does not exist', 104: 'Version not supported',
        105: 'Permission denied (user has no access / no File Station permission)',
        106: 'Session timed out', 107: 'Session interrupted by duplicate login',
        119: 'Session invalid',
        400: 'Wrong username or password / invalid parameter',
        401: 'Account disabled / unknown file operation error',
        402: 'Permission denied / system too busy',
        403: '2-factor code required', 404: 'Wrong 2-factor code',
        407: 'Operation not permitted / IP blocked',
        408: 'No such file or folder (check the path)',
        409: 'File system not supported',
    }
    OFFICE_EXT = ('.osheet', '.odoc', '.oslides')

    def __init__(self, host, bases=None, socks_port=SOCKS_PORT, use_proxy='auto',
                 user_secret='SYNO_USER', pass_secret='SYNO_PASS'):
        _ensure_pysocks()
        import requests
        import urllib3
        urllib3.disable_warnings()

        self.host = host
        self.bases = bases or [f'https://{host}:5001', f'http://{host}:5000',
                               f'https://{host}']
        self.base = None
        self.login_method = None
        self.sid = None
        self.user_secret = user_secret
        self.pass_secret = pass_secret
        self._creds = None

        self.s = requests.Session()
        self.s.verify = False  # Synology usually uses a self-signed certificate
        # 'auto': use the SOCKS5 proxy if our NetBird is running, otherwise go
        # direct (a NetBird app/service on this machine routes 100.x itself)
        if use_proxy == 'auto':
            use_proxy = _port_open(socks_port)
        self.use_proxy = bool(use_proxy)
        if use_proxy:
            proxy = f'socks5h://127.0.0.1:{socks_port}'
            self.s.proxies.update({'http': proxy, 'https': proxy})

    def __repr__(self):
        state = 'logged in' if self.sid else 'not logged in'
        via = 'SOCKS5' if self.use_proxy else 'direct'
        return f'<Synology {self.base or self.host} ({state}, {via})>'

    # ---- connection

    def detect(self):
        """Find which DSM port/method answers the API (no password sent)."""
        probe = {'api': 'SYNO.API.Info', 'version': 1, 'method': 'query',
                 'query': 'SYNO.API.Auth'}
        for base in self.bases:
            for method in ('POST', 'GET'):
                try:
                    url = f'{base}/webapi/query.cgi'
                    r = (self.s.post(url, data=probe, timeout=10) if method == 'POST'
                         else self.s.get(url, params=probe, timeout=10))
                    if (r.headers.get('Content-Type', '').startswith('application/json')
                            and r.json().get('success')):
                        self.base, self.login_method = base, method
                        return base
                except Exception:
                    continue
        raise SynologyError(None, 'Cannot reach DSM API on any port. '
                                  'Is NetBird connected? Try NetBird().status()')

    def login(self, account=None, password=None, otp=None, quiet=False):
        if not self.base:
            self.detect()
        account = account or _get_secret(self.user_secret, 'DSM username: ')
        password = password or _get_secret(self.pass_secret, 'DSM password: ', hidden=True)

        data = {'api': 'SYNO.API.Auth', 'version': 6, 'method': 'login',
                'account': account, 'passwd': password,
                'session': 'FileStation', 'format': 'sid'}
        if otp:
            data['otp_code'] = otp
        url = f'{self.base}/webapi/entry.cgi'
        try:
            r = (self.s.post(url, data=data, timeout=30) if self.login_method == 'POST'
                 else self.s.get(url, params=data, timeout=30)).json()
        except Exception as e:
            # 'from None' keeps the password (possible in a GET URL) out of the traceback
            raise SynologyError(None, f'Login request failed ({type(e).__name__})') from None

        if not r.get('success'):
            code = r.get('error', {}).get('code')
            if code == 403 and not otp:
                return self.login(account, password,
                                  otp=input('2-factor code: ').strip(), quiet=quiet)
            raise SynologyError(code, self.ERRORS.get(code, 'Login failed'))

        self.sid = r['data']['sid']
        self._creds = (account, password)  # kept in memory only, for auto re-login
        if not quiet:
            print(f'Logged in to {self.base} as {account}')
        return self

    def logout(self, quiet=False):
        if self.sid:
            try:
                self.s.get(f'{self.base}/webapi/entry.cgi', timeout=10,
                           params={'api': 'SYNO.API.Auth', 'version': 6, 'method': 'logout',
                                   'session': 'FileStation', '_sid': self.sid})
            except Exception:
                pass
        self.sid, self._creds = None, None
        if not quiet:
            print('Logged out')

    def _api(self, params, retry=True):
        if not self.sid:
            raise SynologyError(None, 'Not logged in, run login() first')
        p = dict(params, _sid=self.sid)
        r = self.s.get(f'{self.base}/webapi/entry.cgi', params=p, timeout=60).json()
        if not r.get('success'):
            code = r.get('error', {}).get('code')
            if code in (106, 107, 119) and retry and self._creds:
                self.login(*self._creds, quiet=True)
                return self._api(params, retry=False)
            raise SynologyError(code, self.ERRORS.get(code, 'Unknown error'))
        return r.get('data', {})

    # ---- browsing

    def ls(self, path='/', show=True, pattern=None):
        """List a folder ('/' lists shared folders). Returns a list of dicts."""
        extra = json.dumps(['size', 'time'])
        if path in ('', '/'):
            data = self._api({'api': 'SYNO.FileStation.List', 'version': 2,
                              'method': 'list_share', 'additional': extra})
            raw = data.get('shares', [])
        else:
            data = self._api({'api': 'SYNO.FileStation.List', 'version': 2,
                              'method': 'list', 'folder_path': path,
                              'additional': extra})
            raw = data.get('files', [])

        items = []
        for f in raw:
            add = f.get('additional', {})
            items.append({
                'name': f['name'], 'path': f['path'], 'isdir': f['isdir'],
                'size': add.get('size', 0) if not f['isdir'] else None,
                'mtime': add.get('time', {}).get('mtime'),
            })
        if pattern:
            items = [i for i in items if fnmatch.fnmatch(i['name'].lower(), pattern.lower())]
        items.sort(key=lambda i: (not i['isdir'], i['name'].lower()))

        if show:
            if not items:
                print('(empty)')
            for i in items:
                size = '' if i['isdir'] else _human(i['size'])
                print(f"{'📁' if i['isdir'] else '📄'} {size:>10}  {i['path']}")
        return items if not show else None

    def find(self, pattern, root='/', max_depth=5, show=True):
        """Search file/folder names (wildcards like '*.xlsx', 'report*')."""
        found, stack = [], [(root, 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                items = self.ls(folder, show=False)
            except SynologyError:
                continue  # skip folders without permission
            for i in items:
                if fnmatch.fnmatch(i['name'].lower(), pattern.lower()):
                    found.append(i)
                    if show:
                        size = '' if i['isdir'] else _human(i['size'])
                        print(f"{'📁' if i['isdir'] else '📄'} {size:>10}  {i['path']}")
                if i['isdir'] and depth < max_depth:
                    stack.append((i['path'], depth + 1))
        if show and not found:
            print('Nothing found')
        return found if not show else None

    # ---- downloading

    def _isdir(self, path):
        """Ask the NAS whether path is a folder.

        Returns True (folder), False (file), or None if it can't be determined.
        A folder always comes back from the Download API as a .zip archive.
        """
        try:
            data = self._api({'api': 'SYNO.FileStation.List', 'version': 2,
                              'method': 'getinfo', 'path': json.dumps([path]),
                              'additional': json.dumps(['type'])})
            files = data.get('files', [])
            if files:
                return bool(files[0].get('isdir'))
        except SynologyError:
            pass
        return None

    def download(self, path, dest=None, overwrite=False, extract=False, _retry=True):
        """Download a file, or a folder (fetched as a single .zip from the NAS).

        A folder is always returned zipped by DSM; this saves it with a .zip
        name even when the server omits a zip Content-Type header.

        extract=True  unzip a folder archive into a folder of the same name
                      next to the .zip, and return that folder's path instead.

        dest           local folder (default: /content in Colab, else the
                       current folder)

        Returns the local path (the .zip, or the extracted folder if extract).
        """
        dest = dest or _default_dest()
        if path.lower().endswith(self.OFFICE_EXT):
            print('Note: this is a Synology Office file; it can only be opened in '
                  'Synology Office. Export it to .xlsx/.docx on the NAS first if needed.')
        if not self.sid:
            raise SynologyError(None, 'Not logged in, run login() first')

        is_dir = self._isdir(path)  # decided before the transfer, not from headers

        def _maybe_extract(zip_path):
            if not (extract and zip_path.lower().endswith('.zip')):
                return zip_path
            import zipfile
            target = zip_path[:-4]
            with zipfile.ZipFile(zip_path) as z:
                z.extractall(target)
            print(f'Extracted -> {target}')
            return target

        os.makedirs(dest, exist_ok=True)
        params = {'api': 'SYNO.FileStation.Download', 'version': 2, 'method': 'download',
                  'path': json.dumps([path]), 'mode': 'download', '_sid': self.sid}

        with self.s.get(f'{self.base}/webapi/entry.cgi', params=params,
                        stream=True, timeout=60) as r:
            ctype = r.headers.get('Content-Type', '')
            if 'application/json' in ctype:
                err = r.json().get('error', {}).get('code')
                if err in (106, 107, 119) and _retry and self._creds:
                    self.login(*self._creds, quiet=True)
                    return self.download(path, dest, overwrite, extract, _retry=False)
                raise SynologyError(err, self.ERRORS.get(err, 'Download failed'))
            r.raise_for_status()

            name = os.path.basename(path.rstrip('/'))
            # A folder (or a zip Content-Type as a fallback) is saved as .zip.
            if (is_dir or 'zip' in ctype) and not name.lower().endswith('.zip'):
                name += '.zip'
            out = os.path.join(dest, name)
            if os.path.exists(out) and not overwrite:
                print(f'Already exists, skipped: {out}  (use overwrite=True)')
                return _maybe_extract(out)

            total = int(r.headers.get('Content-Length', 0))
            done, start = 0, time.time()
            tmp = out + '.part'
            with open(tmp, 'wb') as f, _progress(total, name) as update:
                for chunk in r.iter_content(1024 * 1024):
                    f.write(chunk)
                    done += len(chunk)
                    update(len(chunk))
            os.replace(tmp, out)

        print(f'{name}: {_human(done)} done in {time.time() - start:.1f}s -> {out}')
        return _maybe_extract(out)

    def download_more(self, paths, dest=None, overwrite=False, extract=False):
        results = []
        for p in paths:
            try:
                results.append(self.download(p, dest, overwrite, extract))
            except SynologyError as e:
                print(f'Failed {p}: {e}')
        return results

    # ---- uploading

    def upload(self, local_path, dest_folder, overwrite=False, create_parents=True,
               remote_name=None, show=True, _retry=True):
        """Upload a local file to a NAS folder and return the remote path.

        dest_folder    e.g. '/home/Drive' (a shared folder or a path inside one)
        overwrite      True replaces an existing file; False errors if it exists
        create_parents create dest_folder (and parents) if it doesn't exist
        remote_name    store under a different name than the local file's

        Only single files are supported (the DSM upload API takes one file per
        request); to send a folder, zip it locally first and upload the .zip.
        """
        if not self.sid:
            raise SynologyError(None, 'Not logged in, run login() first')
        if not os.path.isfile(local_path):
            raise SynologyError(None, f'No such local file: {local_path}')

        _ensure_toolbelt()
        from requests_toolbelt.multipart.encoder import (
            MultipartEncoder, MultipartEncoderMonitor)

        name = remote_name or os.path.basename(local_path)
        dest_folder = dest_folder.rstrip('/') or '/'
        total = os.path.getsize(local_path)

        with open(local_path, 'rb') as fh:
            # Order matters: DSM needs the API fields *before* the file part,
            # so build an ordered list of fields rather than a dict.
            encoder = MultipartEncoder(fields=[
                ('api', 'SYNO.FileStation.Upload'),
                ('version', '2'),
                ('method', 'upload'),
                ('path', dest_folder),
                ('create_parents', 'true' if create_parents else 'false'),
                ('overwrite', 'true' if overwrite else 'false'),
                ('file', (name, fh, 'application/octet-stream')),
            ])

            wire_total = encoder.len  # full multipart body size (file + headers)
            start, seen = time.time(), {'n': 0}

            with _progress(wire_total, name, show) as update:
                def _cb(monitor):
                    delta = monitor.bytes_read - seen['n']
                    seen['n'] = monitor.bytes_read
                    if delta:
                        update(delta)

                monitor = MultipartEncoderMonitor(encoder, _cb)
                try:
                    r = self.s.post(f'{self.base}/webapi/entry.cgi',
                                    params={'_sid': self.sid}, data=monitor,
                                    headers={'Content-Type': monitor.content_type},
                                    timeout=(30, None)).json()
                except Exception as e:
                    raise SynologyError(
                        None, f'Upload request failed ({type(e).__name__})') from None

        if not r.get('success'):
            code = r.get('error', {}).get('code')
            if code in (106, 107, 119) and _retry and self._creds:
                self.login(*self._creds, quiet=True)
                return self.upload(local_path, dest_folder, overwrite, create_parents,
                                   remote_name, show, _retry=False)
            raise SynologyError(code, self.ERRORS.get(code, 'Upload failed'))

        remote = f'{dest_folder}/{name}'
        if show:
            print(f'{name}: {_human(total)} uploaded in '
                  f'{time.time() - start:.1f}s -> {remote}')
        return remote

    def upload_more(self, local_paths, dest_folder, overwrite=False, create_parents=True):
        results = []
        for p in local_paths:
            try:
                results.append(self.upload(p, dest_folder, overwrite, create_parents))
            except SynologyError as e:
                print(f'Failed {p}: {e}')
        return results