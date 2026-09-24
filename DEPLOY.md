# Deploying Weekly Shop on a Synology DS920+ (DSM 7.2)

The app runs as one container from `docker-compose.yml`. It listens on
**`127.0.0.1:8420` only**, so nothing on your LAN or the internet can reach it
directly. You reach it through either **Tailscale** (option A, recommended) or
**DSM's reverse proxy over public HTTPS** (option B).

> The session cookie is always `Secure`, so browsers only keep you logged in
> over **HTTPS**. Both options below give you HTTPS. Plain
> `http://nas:8420` is not supported.

---

## 1. Create the folder

1. Install **Container Manager** from Package Center if you haven't already.
   This also creates the `docker` shared folder.
2. In **File Station**, open `docker` and create `weekly-shop`, then create
   `data` inside it. You end up with `/volume1/docker/weekly-shop/data`.
3. Find your DSM user's uid/gid. Enable SSH in **Control Panel → Terminal &
   SNMP**, then run:
   ```sh
   ssh you@your-nas.local
   id
   # uid=1026(you) gid=100(users) ...
   ```
   You'll need these numbers for `PUID`/`PGID` in step 2. The `data` folder
   you created in File Station is already owned by this user.

## 2. Get the compose file

The image is built by GitHub Actions on every push to `main` and published as
**`ghcr.io/mberrido/weekly-shop:latest`**, so the NAS only needs two files in
`/volume1/docker/weekly-shop`: the compose file and your `.env`.

Copy [`deploy/docker-compose.yml`](deploy/docker-compose.yml) and
[`.env.example`](.env.example) there, via SMB (Finder → Go → Connect to Server
→ `smb://your-nas.local/docker`) or over SSH:

```sh
cd /volume1/docker/weekly-shop
curl -fsSLo docker-compose.yml https://raw.githubusercontent.com/mberrido/weekly-shop/main/deploy/docker-compose.yml
curl -fsSLo .env.example https://raw.githubusercontent.com/mberrido/weekly-shop/main/.env.example
```

(For a private repo, download the two files from GitHub in your browser instead.)

**Private image?** If the repository is private, its image is too, and the NAS
has to log in once to pull it:

1. GitHub → Settings → Developer settings → **Personal access tokens (classic)** →
   new token with only the `read:packages` scope.
2. On the NAS: `sudo docker login ghcr.io -u mberrido` and paste the token as the password.

Then create the `.env` file next to `docker-compose.yml`:

```sh
cd /volume1/docker/weekly-shop
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'   # paste as SESSION_SECRET
vi .env
```

Fill in:

| Variable | Notes |
|---|---|
| `APP_PIN` | **Required.** The household login: exactly 6 digits. See the note under option B if the app is on the public internet. |
| `SESSION_SECRET` | **Required.** At least 32 bytes. Use the generated value. |
| `COOKIDOO_EMAIL` / `COOKIDOO_PASSWORD` | Optional. Leave blank and the Cookidoo buttons are hidden. |
| `PUID` / `PGID` | The numbers from `id` in step 1. |
| `PEXELS_API_KEY` | Optional. Free key from [pexels.com/api](https://www.pexels.com/api/). Meals without a photo get one automatically, and the meal editor gets a **Search online** button. |
| `PANTRY_URL` | Optional. Your Pantry Tracker, e.g. `http://192.168.1.20:1683`. Use the NAS's **LAN IP**: inside a container `localhost` is the container itself. Weekly Shop only reads from it. |

Wrap any value containing `$`, `#` or spaces in **single quotes**, e.g.
`COOKIDOO_PASSWORD='purple $kettle'`. Compose would otherwise
treat `$kettle` as a variable. Lock the file down: `chmod 600 .env`.

The app refuses to start if `APP_PIN` isn't exactly 6 digits, or `SESSION_SECRET` is
missing or too short. It logs exactly which one, so check the container log if it keeps
restarting.

## 3. Start it

### Container Manager (GUI)

1. **Container Manager → Project → Create**.
2. Project name `weekly-shop`, path `/docker/weekly-shop`.
3. Source: **Use existing docker-compose.yml**. Click Next.
4. Skip the Web Station portal settings. Click Next, then **Done**.
   It pulls the image, which takes a minute.
5. The container should show **Healthy** under Container.

### Or over SSH

DSM's Docker uses `docker-compose` (with a hyphen). On a standard Docker install it's `docker compose`.

```sh
cd /volume1/docker/weekly-shop
sudo docker-compose pull
sudo docker-compose up -d
sudo docker-compose ps          # STATUS should say (healthy)
curl -s http://127.0.0.1:8420/healthz   # {"ok":true}
```

## 4. Updating

**Automatic (Watchtower).** Push to `main`; GitHub Actions runs the tests, then
builds and publishes a new `latest` image. Watchtower on the NAS (the same one
that updates Pantry Tracker) notices the new image, pulls it and restarts the
container. The compose file already has the
`com.centurylinklabs.watchtower.enable=true` label, for Watchtower running with
`--label-enable`. For a private image, Watchtower needs the same `ghcr.io`
login as above (mount `/root/.docker/config.json:/config.json:ro` into its
container, or set `REPO_USER` / `REPO_PASS`).

**Manually:**
- **GUI:** Container Manager → Project → `weekly-shop` → **Action → Stop**,
  then Image → `ghcr.io/mberrido/weekly-shop` → **Update** (or pull), then start the project.
- **SSH:** `cd /volume1/docker/weekly-shop && sudo docker-compose pull && sudo docker-compose up -d`

**Roll back:** change the image tag in `docker-compose.yml` from `latest` to an
earlier `sha-…` tag (listed on the package page on GitHub) and run
`sudo docker-compose up -d`.

Your data in `data/` is never touched by updates. The database schema is
upgraded automatically on start.

**Build locally instead?** The repo's own `docker-compose.yml` builds from
source (`sudo docker-compose up -d --build`) if you'd rather not use the registry.

## 5. Backups with Hyper Backup

The app writes a consistent snapshot of the database every day to
`data/backups/shop-YYYY-MM-DD.db` and keeps the last 14. A running SQLite file
can be caught mid-write, but these snapshots can't, so they're what you
restore from.

1. **Hyper Backup → + → Data backup task**.
2. Pick a destination: USB disk, another Synology, or C2/cloud.
3. Select the folder `docker/weekly-shop`. This includes `data/` (database, snapshots and meal photos in `data/photos/`) and `.env`.
4. **Enable client-side encryption**, because `.env` holds your passwords.
5. Schedule it daily, e.g. 03:30.

**Restore:** stop the project, then copy the snapshot you want over the live
database and restart:

```sh
sudo docker-compose stop
cp data/backups/shop-2026-09-20.db data/shop.db
sudo docker-compose start
```

---

## Remote access

### Option A: Tailscale (recommended: nothing exposed to the internet)

Only devices signed in to your tailnet can reach the NAS. There are no router
changes and no public attack surface.

1. **Package Center → Tailscale → Install**, open it and sign in.
2. In the Tailscale admin console (**DNS** page), enable **MagicDNS** and
   **HTTPS Certificates**.
3. On the NAS, over SSH, publish the app on the tailnet with HTTPS:
   ```sh
   sudo tailscale serve --bg 8420
   sudo tailscale serve status
   # https://your-nas.your-tailnet.ts.net/ -> proxy http://127.0.0.1:8420
   ```
   `--bg` makes it persist across reboots.
4. Install the **Tailscale** app on each phone and sign in. Use the same
   account, or share the NAS machine with family members' accounts from the
   admin console.
5. On each phone, open `https://your-nas.your-tailnet.ts.net`, log in, then
   **Share → Add to Home Screen** (iPhone) or **⋮ → Install app** (Android).

> Your brief suggested `http://<nas-tailscale-name>:8420`. That URL can't work
> with this setup: the port is bound to localhost, and browsers won't store the
> `Secure` session cookie over plain http. `tailscale serve` fixes both and
> gives you a real certificate, so it's required here rather than optional.

### Option B: Public HTTPS via your synology.me DDNS

Anyone on the internet can reach the login page. It's protected by
`APP_PIN`, a lockout after 5 failed attempts per IP per 15 minutes,
`Secure`/`HttpOnly`/`SameSite=Lax` cookies and HSTS.

> **PIN caveat:** a 6-digit PIN has a million combinations. The per-IP lockout
> stops one attacker guessing it, but someone with many IP addresses could
> eventually. That's why Tailscale (option A) is recommended with a PIN.

1. **Certificate:** go to **Control Panel → Security → Certificate**. Open the
   Let's Encrypt certificate for `<name>.synology.me` and check whether it
   covers `*.<name>.synology.me`. If it doesn't, **Add → Replace an existing
   certificate** (or add a new one) and choose Synology's certificate for your
   DDNS hostname, which includes the wildcard for `synology.me` names.
2. **Reverse proxy:** go to **Control Panel → Login Portal → Advanced →
   Reverse Proxy → Create**.
   - Description: `weekly-shop`
   - Source: Protocol **HTTPS**, Hostname `shop.<name>.synology.me`,
     Port **443**, tick **Enable HSTS**.
   - Destination: Protocol **HTTP**, Hostname `localhost`, Port **8420**.
   - **Custom Header** tab: **Create → WebSocket**. Then add:
     - `X-Forwarded-For` = `$proxy_add_x_forwarded_for`
     - `X-Forwarded-Proto` = `$scheme`
3. **Assign the certificate:** go to **Control Panel → Security → Certificate →
   Settings** and pick the wildcard certificate for the
   `shop.<name>.synology.me` entry.
4. **Router:** forward **TCP 443** to the NAS if it isn't already. **Don't
   forward 8420** (it's bound to localhost anyway), and don't forward
   5000/5001.
5. **Hardening:**
   - **Control Panel → Security → Protection → Auto Block**: enable it.
     Older guides call this Security → Account. It protects DSM logins; the
     app has its own lockout.
   - Optional: **Control Panel → Security → Firewall**. Enable it and create
     rules in this order: allow all from your LAN subnet
     (e.g. `192.168.1.0/24`), allow port 443 from **Location: United
     Kingdom**, then deny all. Add the LAN rule first so you don't lock
     yourself out.
6. **Fallback** if the subdomain/wildcard route isn't available: use Source
   `HTTPS`, Hostname `<name>.synology.me`, Port **8443**, with the same
   destination. Forward TCP **8443** on the router. The URL becomes
   `https://<name>.synology.me:8443`.

**How the app sees real client IPs:** DSM's nginx connects to the container
through Docker's bridge gateway (`172.30.84.1`, fixed in
`docker-compose.yml`). Uvicorn trusts `X-Forwarded-For` only from that
address and `127.0.0.1`, and takes the rightmost untrusted hop, so
the lockout applies to the real client and can't be dodged by sending a fake
header. If `172.30.84.0/24` clashes with another network on your NAS, change
the subnet and `FORWARDED_ALLOW_IPS` together.

#### About `http://` → `https://`

DSM's reverse-proxy UI has no "redirect HTTP to HTTPS" rule for custom
hostnames. The safe setup is to **not forward port 80**. Then:

- `http://shop.<name>.synology.me` fails to connect instead of showing a
  login form over plain HTTP.
- After the first HTTPS visit, HSTS (set by both DSM and the app) makes the
  browser upgrade any `http://` link to `https://` automatically.
- The home-screen app always opens the `https://` URL.

Don't add an HTTP-source reverse-proxy rule to "fix" this: it would serve the
login form, and your password, over plain HTTP.

#### End-to-end test (on mobile data, not home Wi-Fi)

| Check | Expected |
|---|---|
| `https://shop.<name>.synology.me` | Padlock shows a valid certificate for the hostname |
| `http://shop.<name>.synology.me` (first time, no HSTS yet) | Fails to connect, or upgrades to https. Never a login form over http |
| `http://…` again after one https visit | Browser upgrades to `https://` itself (HSTS) |
| `https://…/api/meals` while logged out | `{"detail":"Not logged in"}` with status 401 |
| 5 wrong passwords | 6th attempt says "Too many attempts", even with the right password, for 15 min |
| Log in, fully close the browser/app, reopen | Still logged in. The session lasts 90 days and renews with daily use |
| From a laptop: `curl -sI https://shop.<name>.synology.me/login` | Shows `strict-transport-security`, `x-content-type-options`, `content-security-policy: frame-ancestors 'none'…` |

The lockout counter lives in memory, so restarting the container clears it.
