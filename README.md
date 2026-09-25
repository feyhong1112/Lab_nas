# Lab_nas

Access a **Synology NAS** from **Google Colab** (or any headless Linux box)
through a **NetBird** mesh network.

`Lab_nas` bundles two small pieces:

- **`NetBird`** — starts the [NetBird](https://netbird.io) client in userspace
  (netstack) mode, connects with a setup key, and exposes a local SOCKS5 proxy.
  On Colab it keeps the peer identity in Google Drive, so every runtime
  reconnects as the same peer with the same NetBird IP.
- **`Synology`** — a Synology File Station client that talks to DSM *through*
  that SOCKS5 proxy: browse, search, download (files and folders as `.zip`),
  and upload, with live progress bars.

## Install

```bash
pip install Lab_nas
```

The NetBird **binary** itself is downloaded automatically the first time you
call `NetBird().start()` (fetched from the official `netbirdio/netbird` GitHub
releases).

## Quick start (Google Colab)

```python
from Lab_nas import NetBird, Synology

NetBird().start()                       # connect Colab to your NetBird network
nas = Synology('100.83.14.114')         # your NAS's NetBird IP
nas.login()                             # uses Colab secrets SYNO_USER / SYNO_PASS

nas.ls('/')                             # list shared folders
nas.find('*.xlsx', '/home/Drive')       # search by name
nas.download('/home/Drive/file.xlsx')   # download to /content
nas.download('/home/Drive/MyFolder')    # a folder arrives as MyFolder.zip
nas.upload('/content/file.xlsx', '/home/Drive')   # upload a local file
```

### Secrets

In Colab, add these in the 🔑 sidebar (with *Notebook access* enabled). If a
secret is missing you'll simply be prompted to type it.

| Secret              | Meaning                        |
| ------------------- | ------------------------------ |
| `NETBIRD_SETUP_KEY` | a reusable NetBird setup key   |
| `SYNO_USER`         | DSM username                   |
| `SYNO_PASS`         | DSM password                   |

## Common operations

```python
nb = NetBird(hostname='colab')          # customise the peer name
nb.start()
nb.status()                             # show NetBird status + proxy state
nb.log()                                # tail the NetBird log
nb.stop()

nas = Synology('100.83.14.114')
nas.login('admin', 'password')          # or pass credentials explicitly
nas.ls('/home')
nas.find('report*', '/home', max_depth=3)
nas.download_more(['/home/a.pdf', '/home/b.pdf'])
nas.download('/home/Photos', extract=True)   # download + unzip the folder
nas.upload('/content/out.csv', '/home/Drive', overwrite=True)
nas.logout()
```

## Requirements

- Linux (Colab counts). The NetBird binary published for `linux_amd64` /
  `linux_arm64` is used.
- A running NetBird network and a **setup key**.
- A Synology NAS reachable on your NetBird network, with an account that has
  File Station permission.

## Notes

- Runs NetBird in **netstack** (userspace) mode, so no `tun` device or kernel
  module is needed — which is exactly what makes it work inside Colab.
- Synology often serves DSM with a self-signed certificate; TLS verification is
  disabled for the NAS session by design.
- Synology Office files (`.osheet`, `.odoc`, `.oslides`) can only be opened in
  Synology Office — export to `.xlsx`/`.docx` on the NAS first if you need them
  elsewhere.

## License

MIT — see [LICENSE](LICENSE).
