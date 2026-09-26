# Lab_nas

Access a **Synology NAS** from **Google Colab, Linux or Windows** through a
**NetBird** mesh network, from Python or straight from bash / cmd.

`Lab_nas` bundles two small pieces:

- **`NetBird`** — starts the [NetBird](https://netbird.io) client in userspace
(netstack) mode and exposes a local SOCKS5 proxy. It saves the peer identity,
so later runs reconnect as the same peer **without asking for the setup key**.
If a NetBird app/service on the machine is already connected, it just uses it.
- **`Synology`** — a Synology File Station client: browse, search, download
(files and folders as `.zip`) and upload, with live progress bars.

## Install

```
pip install Lab_nas
```

The NetBird binary is downloaded automatically on first start (from the
official `netbirdio/netbird` GitHub releases) if `netbird` isn't installed.

## How connecting works

`NetBird().start()` / `lab_nas start` tries these in order and stops at the
first that works:

1. NetBird is **already connected** (Lab_nas's own daemon, or a NetBird
   app/service you set up in a terminal) → nothing to do.
2. A **saved identity** exists → reconnect with it, no setup key needed.
3. A setup key is **already available** (argument, `NETBIRD_SETUP_KEY`
   environment variable, or Colab secret) → used without asking.
4. It failed (bad/expired key, peer deleted) or there was **no answer within
   60 seconds** → you are asked to type the setup key (3 tries).

## Where the identity is saved

| Platform | Folder |
| -------- | ------ |
| Colab    | `/content/drive/MyDrive/netbird` (Google Drive) |
| Linux    | `~/.config/Lab_nas/netbird` (or `$XDG_CONFIG_HOME/Lab_nas/netbird`) |
| Windows  | `Documents\Lab_nas\netbird` |

Change it with `NetBird(config_dir=...)`, `--config-dir`, or the
`LAB_NAS_CONFIG_DIR` environment variable. `config.json` contains the peer's
private key — keep it private (careful if Documents syncs to OneDrive).

## Command line (bash and cmd)

```
lab_nas start                                   # connect
lab_nas status
lab_nas ls / --host 100.83.14.114
lab_nas find "*.xlsx" /home/Drive --host 100.83.14.114
lab_nas download /home/Drive/file.xlsx /home/Drive/MyFolder --dest .
lab_nas upload report.xlsx --to /home/Drive
lab_nas stop
```

If `lab_nas` isn't found, use `python -m Lab_nas ...` (Windows: `py -m Lab_nas ...`).
Set values once instead of typing them each time:

```bash
# bash
export NETBIRD_SETUP_KEY=XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
export LAB_NAS_HOST=100.83.14.114
export SYNO_USER=admin
```

```bat
:: cmd
set NETBIRD_SETUP_KEY=XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX
set LAB_NAS_HOST=100.83.14.114
set SYNO_USER=admin
```

Missing values (like `SYNO_PASS`) are asked for. Run `lab_nas <command> -h`
for all options.

## Quick start (Python / Colab)

```
from Lab_nas import NetBird, Synology

NetBird().start()                       # connect to your NetBird network
nas = Synology('100.83.14.114')         # your NAS's NetBird IP
nas.login()                             # uses SYNO_USER / SYNO_PASS

nas.ls('/')                             # list shared folders
nas.find('*.xlsx', '/home/Drive')       # search by name
nas.download('/home/Drive/file.xlsx')   # Colab: /content, else current folder
nas.download('/home/Drive/MyFolder')    # a folder arrives as MyFolder.zip
nas.upload('file.xlsx', '/home/Drive')  # upload a local file
```

### Secrets

Looked up as an environment variable first, then a Colab secret (🔑 sidebar
with *Notebook access* on). If missing, you're asked to type it.

| Secret              | Meaning                      |
| ------------------- | ---------------------------- |
| `NETBIRD_SETUP_KEY` | a reusable NetBird setup key |
| `SYNO_USER`         | DSM username                 |
| `SYNO_PASS`         | DSM password                 |

## Common operations

```
nb = NetBird(hostname='colab', timeout=60)   # peer name, seconds before asking for the key
nb.start()
nb.status()                             # NetBird status + proxy state
nb.log()                                # tail the NetBird log
nb.stop()

nas = Synology('100.83.14.114')         # proxy used automatically when needed
nas.login('admin', 'password')
nas.ls('/home')
nas.find('report*', '/home', max_depth=3)
nas.download_more(['/home/a.pdf', '/home/b.pdf'])
nas.download('/home/Photos', extract=True)
nas.upload('out.csv', '/home/Drive', overwrite=True)
nas.logout()
```

## Requirements

- Colab, Linux (amd64/arm64) or Windows (amd64/arm64).
- A NetBird network and a **setup key** (needed only the first time).
- A Synology NAS on your NetBird network, with an account that has File
Station permission.

## Notes

- Runs NetBird in **netstack** (userspace) mode on its own daemon address
(`127.0.0.1:41799`), so no `tun` device is needed and a NetBird app already
installed on the machine is never touched.
- On Windows, if the daemon doesn't start, try cmd **as Administrator**.
- Synology often serves DSM with a self-signed certificate; TLS verification is
disabled for the NAS session by design.
- Synology Office files (`.osheet`, `.odoc`, `.oslides`) can only be opened in
Synology Office — export to `.xlsx`/`.docx` on the NAS first if you need them
elsewhere.

## License

MIT — see [LICENSE](LICENSE).
