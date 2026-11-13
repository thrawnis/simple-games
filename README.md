# Simple Games Engine

A tiny, locked-down web app for hosting single-file HTML games.

- **Public site** (`/`): lists every game alphabetically. Clicking one serves
  the **latest** version of its HTML file directly (`/play/<id>`).
- **Admin area** (`/admin`): password protected. Create games, upload new
  versions, rename, delete, download old versions, or restore an old version
  (which re-publishes it as a new latest version).
- **History**: the last **50** uploads of each game are kept; older ones are
  deleted automatically. Only the newest version is ever playable.
- **Storage**: plain files in `./data` on the host server.

## Quick start

Requirements: Docker with the Compose plugin, on Linux.

```bash
git clone <this repo> simple-games && cd simple-games
./setup.sh                    # creates settings.env (random strong password) and ./data
docker compose up -d --build
```

Open `http://<server>:8080/` for the site and `http://<server>:8080/admin`
for the admin area. The admin password was printed by `setup.sh` and is in
`settings.env`.

To change the password, edit `ADMIN_PASSWORD` in `settings.env` and run
`docker compose up -d` (this recreates the container with the new settings).
The app **refuses to start** if the password is weak: it needs 16+
characters including 3 of lowercase / uppercase / digits / symbols, or 24+
characters of anything.

### HTTPS (strongly recommended)

The admin password travels over the connection, so put the site behind HTTPS
before using it over the internet. The easiest way is a reverse proxy such as
Caddy on the same host:

1. In `docker-compose.yml` change the port to `"127.0.0.1:8080:8080"`.
2. Point Caddy (or nginx/Traefik) at `127.0.0.1:8080`, e.g. a Caddyfile:
   `games.example.com { reverse_proxy 127.0.0.1:8080 }`
3. Set `COOKIE_SECURE=true` in `settings.env` and `docker compose up -d`.

## Files on the host

```
data/
  games/<game-id>/meta.json            name + version history
  games/<game-id>/versions/000001.html one file per kept upload
```

Back up the `data/` folder to back up everything. `settings.env` holds your
password; keep it `chmod 600` and never commit it (it is git-ignored).

## Settings (`settings.env`)

| Setting | Default | Meaning |
|---|---|---|
| `ADMIN_PASSWORD` | – | Admin password (required, must be strong) |
| `SECRET_KEY` | random per start | Signs the admin login cookie |
| `COOKIE_SECURE` | `false` | Set `true` once served over HTTPS |
| `SITE_TITLE` | `Simple Games` | Shown in the header |
| `MAX_UPLOAD_KB` | `5120` | Max size of one HTML file (also raise `client_max_body_size` in `nginx.conf` if you go above ~5.5 MB) |
| `KEEP_VERSIONS` | `50` | Versions kept per game |
| `ADMIN_SESSION_HOURS` | `8` | Admin login lifetime |
| `LOGIN_MAX_FAILURES` / `LOGIN_LOCKOUT_MINUTES` | `5` / `15` | Lock out an IP after repeated wrong passwords |
| `GAMES_ALLOW_EXTERNAL_RESOURCES` | `false` | Let games load files / call APIs over https |

## Security design

The goal: even if a game file is malicious, or the app has a bug, nothing
can get from the app to the host machine.

**Container isolation (the app cannot touch the host)**

- The app container has **no network at all** (`network_mode: none`). It can't
  reach the internet, your LAN, or any service on the host. Its only
  interface is loopback. An nginx container is the only thing exposed, and it
  talks to the app over a Unix socket on a private Docker volume.
  (Docker's `internal: true` networks are *not* enough: they still let a
  container reach the host's own IP on that bridge — tested.)
- The **only host folder** mounted into the app is `./data`. No Docker
  socket, no host paths, no host network, not privileged.
- Both containers run as **non-root** users, with a **read-only filesystem**
  (only a small `noexec` `/tmp` is writable), **all Linux capabilities
  dropped**, `no-new-privileges`, and memory / CPU / process-count limits.
- The app image has `pip` removed and its code files are not writable.

**Game files**

- Uploads must be `.html`/`.htm`, UTF-8 text, no binary data, within the size
  limit. Files are stored under server-generated names (`000001.html`), and
  game ids are restricted to `a-z 0-9 -`, so path tricks like `../` are
  impossible.
- Games are served with a `Content-Security-Policy: sandbox` header **without**
  `allow-same-origin`. The game's JavaScript runs, but in an isolated,
  anonymous origin: it **cannot read the admin cookie, cannot act as the
  admin**, and cannot read other pages of the site.
- By default games **can't contact any server** (no trackers, no data
  exfiltration). Everything must be inside the single HTML file — inline
  scripts, CSS, and `data:` images/sounds all work.
- Old versions are only available to the admin, and only as downloads (never
  rendered in the browser).

**Admin login**

- Strong password enforced at start-up; compared in constant time.
- Brute-force protection: per-IP lockout after 5 failures, plus nginx rate
  limiting on the login URL.
- Session cookie is `HttpOnly`, `SameSite=Strict`, scoped to `/admin`, signed,
  and expires. All admin actions require a CSRF token.
- Strict security headers on every page (CSP, no framing, no sniffing,
  no referrer).

**Things to know**

- Because games are sandboxed, browser storage (`localStorage`, cookies,
  IndexedDB) is **not available** to games. A game that saves high scores with
  `localStorage` should wrap that in `try { … } catch {}` so it still runs.
- If you set `GAMES_ALLOW_EXTERNAL_RESOURCES=true`, games may load scripts from
  CDNs and call web APIs. That is convenient but lets a game contact other
  servers. The sandbox still protects the admin area and the host either way.
- For an extra layer you can enable Docker
  [user-namespace remapping](https://docs.docker.com/engine/security/userns-remap/)
  on the host, so container UIDs never map to real host users.
- Keep the host and images updated: `docker compose pull && docker compose up -d --build`.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r app/requirements.txt pytest
pytest tests
```
