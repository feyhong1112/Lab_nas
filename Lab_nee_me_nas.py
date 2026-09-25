"""
synonb.py - Access a Synology NAS from Google Colab through NetBird.

Put this file in Google Drive (e.g. MyDrive/netbird/synonb.py), then in Colab:

    import sys; sys.path.append('/content/drive/MyDrive/netbird')
    from synonb import NetBird, Synology

    NetBird().start()                      # connect Colab to NetBird
    nas = Synology('100.83.14.114')
    nas.login()                            # uses Colab secrets SYNO_USER / SYNO_PASS
    nas.ls('/')                            # list shared folders
    nas.find('*.xlsx', '/home/Drive')      # search by name
    nas.download('/home/Drive/file.xlsx')  # download to /content
    nas.download('/home/Drive/MyFolder')   # a folder arrives as MyFolder.zip
    nas.upload('/content/file.xlsx', '/home/Drive')  # upload a local file

Colab secrets used (🔑 sidebar, with Notebook access on):
    NETBIRD_SETUP_KEY   reusable NetBird setup key
    SYNO_USER           DSM username
    SYNO_PASS           DSM password
If a secret is missing, you are asked to type it instead.
"""

import os
import sys
import json
import time
import shutil
import socket
import fnmatch
import subprocess
import urllib.request
from contextlib import contextmanager

SOCKS_PORT = 1080
SOCK_FILE = '/var/run/netbird.sock'
LOG_FILE = '/content/netbird.log'
LOCAL_CFG = '/etc/netbird/config.json'


# ----------------------------------------------------------------- helpers

def _in_colab():
    try:
        import google.colab  # noqa: F401
        return True
    except ImportError:
        return False


def _get_secret(name, prompt=None, hidden=False):
    """Read a Colab secret; if it doesn't exist, ask the user."""
    if _in_colab():
        try:
            from google.colab import userdata
            return userdata.get(name)
        except Exception:
            pass
    if prompt is None:
        raise RuntimeError(f'Secret {name} not found')
    if hidden:
        from getpass import getpass
        return getpass(prompt)
    return input(prompt)


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
    """Runs NetBird in userspace (netstack) mode, suitable for Colab.

    The peer identity is kept in Google Drive, so every runtime reconnects
    as the same peer (same name, same NetBird IP).
    """

    def __init__(self, drive_dir='/content/drive/MyDrive/netbird', hostname='colab',
                 socks_port=SOCKS_PORT, setup_key_secret='NETBIRD_SETUP_KEY'):
        self.drive_dir = drive_dir
        self.hostname = hostname
        self.socks_port = socks_port
        self.setup_key_secret = setup_key_secret
        self._log = None

    @property
    def drive_cfg(self):
        return os.path.join(self.drive_dir, 'config.json') if self.drive_dir else None

    def _mount_drive(self):
        if (self.drive_dir and self.drive_dir.startswith('/content/drive')
                and not os.path.isdir('/content/drive/MyDrive')):
            from google.colab import drive
            drive.mount('/content/drive')
        if self.drive_dir:
            os.makedirs(self.drive_dir, exist_ok=True)

    def install(self):
        if shutil.which('netbird'):
            return
        print('Installing NetBird...')
        api = 'https://api.github.com/repos/netbirdio/netbird/releases/latest'
        tag = json.load(urllib.request.urlopen(api))['tag_name']
        ver = tag.lstrip('v')
        url = (f'https://github.com/netbirdio/netbird/releases/download/'
               f'{tag}/netbird_{ver}_linux_amd64.tar.gz')
        urllib.request.urlretrieve(url, '/tmp/nb.tar.gz')
        subprocess.run(['tar', '-xzf', '/tmp/nb.tar.gz', '-C', '/usr/local/bin', 'netbird'],
                       check=True)
        print(f'NetBird {ver} installed')

    def running(self):
        return (os.path.exists(SOCK_FILE) and
                subprocess.run(['pgrep', '-f', 'netbird service run'],
                               capture_output=True).returncode == 0)

    def stop(self, quiet=False):
        subprocess.run(['pkill', '-f', 'netbird service run'], capture_output=True)
        subprocess.run(['pkill', '-f', 'netbird up'], capture_output=True)
        time.sleep(2)
        if os.path.exists(SOCK_FILE):
            os.remove(SOCK_FILE)
        if not quiet:
            print('NetBird stopped')

    def log(self, lines=40):
        try:
            with open(LOG_FILE) as f:
                print(''.join(f.readlines()[-lines:]))
        except FileNotFoundError:
            print('No log file yet')

    def status(self):
        subprocess.run(['netbird', 'status'])
        print(f'SOCKS5 proxy on {self.socks_port}:',
              'listening' if _port_open(self.socks_port) else 'NOT listening')

    def start(self, setup_key=None, timeout=60):
        """Start the daemon and connect. Safe to run again at any time."""
        self._mount_drive()
        self.install()

        # Restore saved identity so this is the same peer as last time
        os.makedirs(os.path.dirname(LOCAL_CFG), exist_ok=True)
        if self.drive_cfg and os.path.exists(self.drive_cfg):
            shutil.copy(self.drive_cfg, LOCAL_CFG)
            print('Restored saved NetBird identity')

        self.stop(quiet=True)

        env = dict(os.environ, NB_USE_NETSTACK_MODE='true',
                   NB_SOCKS5_LISTENER_PORT=str(self.socks_port))
        self._log = open(LOG_FILE, 'w')
        subprocess.Popen(['netbird', 'service', 'run', '--config', LOCAL_CFG,
                          '--log-file', 'console'],
                         env=env, stdout=self._log, stderr=self._log,
                         start_new_session=True)

        for _ in range(20):
            if os.path.exists(SOCK_FILE):
                break
            time.sleep(1)
        else:
            self.log()
            raise RuntimeError('NetBird daemon did not start (log above)')

        key = setup_key or _get_secret(self.setup_key_secret, 'NetBird setup key: ',
                                       hidden=True)
        try:
            r = subprocess.run(['netbird', 'up', '--setup-key', key,
                                '--hostname', self.hostname],
                               capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            self.log()
            raise RuntimeError('netbird up timed out (log above)') from None
        if r.returncode != 0:
            print(r.stdout, r.stderr)
            raise RuntimeError('netbird up failed')

        # Save identity back to Drive
        time.sleep(2)
        if self.drive_cfg and os.path.exists(LOCAL_CFG):
            shutil.copy(LOCAL_CFG, self.drive_cfg)

        for _ in range(15):
            if _port_open(self.socks_port):
                break
            time.sleep(1)
        else:
            self.log()
            raise RuntimeError(f'SOCKS5 proxy not listening on {self.socks_port}')

        print(f'NetBird connected as "{self.hostname}", SOCKS5 proxy on {self.socks_port}')
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

    def __init__(self, host, bases=None, socks_port=SOCKS_PORT, use_proxy=True,
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
        if use_proxy:
            proxy = f'socks5h://127.0.0.1:{socks_port}'
            self.s.proxies.update({'http': proxy, 'https': proxy})

    def __repr__(self):
        state = 'logged in' if self.sid else 'not logged in'
        return f'<Synology {self.base or self.host} ({state})>'

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

    def logout(self):
        if self.sid:
            try:
                self.s.get(f'{self.base}/webapi/entry.cgi', timeout=10,
                           params={'api': 'SYNO.API.Auth', 'version': 6, 'method': 'logout',
                                   'session': 'FileStation', '_sid': self.sid})
            except Exception:
                pass
        self.sid, self._creds = None, None
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

    def download(self, path, dest='/content', overwrite=False, extract=False, _retry=True):
        """Download a file, or a folder (fetched as a single .zip from the NAS).

        A folder is always returned zipped by DSM; this saves it with a .zip
        name even when the server omits a zip Content-Type header.

        extract=True  unzip a folder archive into a folder of the same name
                      next to the .zip, and return that folder's path instead.

        Returns the local path (the .zip, or the extracted folder if extract).
        """
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

    def download_more(self, paths, dest='/content', overwrite=False, extract=False):
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