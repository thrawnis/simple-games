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

The stack listens on port **35838**. Point Nginx Proxy Manager / Cloudflare
Tunnel at `<server IP>:35838` (see below), then open `https://<your-hostname>/`
for the site and `https://<your-hostname>/admin` for the admin area. The admin
password was printed by `setup.sh` and is in `settings.env`.

To change the password, edit `ADMIN_PASSWORD` in `settings.env` and run
`docker compose up -d` (this recreates the container with the new settings).
The app **refuses to start** if the password is weak: it needs 16+
characters including 3 of lowercase / uppercase / digits / symbols, or 24+
characters of anything.

### Updating

On the Docker host, from anywhere:

```bash
./rebuild.sh                     # pull origin/main; rebuild + restart only if changed
./rebuild.sh --force             # rebuild + restart regardless
./rebuild.sh --pull-base-images  # also fetch a newer python base image (slower)
```

It discards local edits to tracked files (`settings.env` and `data/` are
untracked and left alone), builds with `docker buildx`, stamps the image with
the git commit, and skips the rebuild when the running image already matches.

### HTTPS: Nginx Proxy Manager (local) + Cloudflare Tunnel (external)

```
LAN users ──https──► Nginx Proxy Manager ─┐
                                          ├─► :35838 (this stack's nginx) ─► app (no network)
Internet ──https──► Cloudflare ─► cloudflared ─┘
```

Both routes are HTTPS, so `COOKIE_SECURE=true` (the default) is correct.

**Nginx Proxy Manager** – add a Proxy Host:
- Domain: e.g. `games.home.lan` · Scheme `http` · Forward host: this server's
  IP · Forward port `35838`
- SSL tab: your certificate, enable *Force SSL*. Websockets/caching: off.
- Use this local address for the admin area: `https://games.home.lan/admin`.

**Cloudflare Tunnel** – add a Public Hostname:
- e.g. `games.example.com` → Service `HTTP` → `<server IP>:35838`
  (or point it at NPM if you prefer — both work).
- In the Cloudflare dashboard for that hostname, **turn off Rocket Loader**
  (Speed → Optimization). It rewrites `<script>` tags and would stop games
  from running. Email Obfuscation can also be turned off. Other features are
  fine.

**Admin is local-only by default.** Any request that arrives through
Cloudflare (it always carries Cloudflare's `CF-Ray` / `CF-Connecting-IP`
headers, which visitors can't strip) gets a 404 for `/admin`. The public game
list and games work normally over the tunnel. If you really want to manage
games from outside, set `ADMIN_VIA_CLOUDFLARE=true` **and** protect
`games.example.com/admin*` with a Cloudflare Access (Zero Trust) policy, so
only your account can even reach the login page.

**Real client IPs.** This stack's nginx trusts `X-Forwarded-For` only from
private-network addresses (where NPM and cloudflared run) and picks the
right-most address that isn't one of them, which a visitor can't forge. That
is what the login lockout and rate limits use. To tighten it, replace the
`set_real_ip_from` ranges in `nginx.conf` with the exact IPs of your NPM and
cloudflared hosts/containers.

**Port 35838.** Only NPM and cloudflared need to reach it. Firewall it so
nothing else on your LAN can connect (e.g. `ufw allow from <NPM-IP> to any
port 35838`), or, if NPM and cloudflared run on this same machine with host
networking, change the port in `docker-compose.yml` to `"127.0.0.1:35838:8080"`.
Logging in via plain `http://<ip>:35838` won't work while `COOKIE_SECURE=true` –
that's deliberate.

HSTS isn't set by the app; turn it on in Cloudflare (SSL/TLS → Edge
Certificates) and/or NPM if you want it.

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
| `COOKIE_SECURE` | `true` | Login cookie only sent over HTTPS |
| `ADMIN_VIA_CLOUDFLARE` | `false` | Allow `/admin` through Cloudflare Tunnel (use with Cloudflare Access) |
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
- Admin area refused over Cloudflare Tunnel by default (local network only).
- Brute-force protection: per-IP lockout after 5 failures (using the real
  client IP from NPM / Cloudflare), plus nginx rate limiting on the login URL.
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
