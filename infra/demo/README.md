# Public demo site

The docker compose stack in the repo root is a synthetic demo. This directory holds what it
takes to expose that same stack on the internet as a self-resetting public demo.

## What demo site mode is

With `CMMS_DEMO_MODE=1` the app:

- shows a banner at the top of every page ("Demo site · synthetic data · resets every N
  minutes");
- lists the three demo accounts on the login page, each with a one-click sign-in button.

Nothing else changes. Every governance check, fail-closed default and audit record works
exactly as it does without the flag.

## Env knobs

| Variable | Default | Purpose |
|---|---|---|
| `CMMS_DEMO_MODE` | `0` (compose sets `1`) | Banner + one-click demo accounts on the login page. |
| `CMMS_DEMO_RESET_MINUTES` | `30` | The interval the banner announces. Must match the timer below. |
| `CMMS_PUBLIC_BASE_URL` | `http://localhost:8000` | Base for links in notifications. Override from the shell: `CMMS_PUBLIC_BASE_URL=https://work.example.com docker compose up -d` |

## Install the reset timer (systemd)

Assumes the repo is checked out at `/opt/shopfloor-cmms` (edit the unit if not) and that
the user running the units can talk to Docker.

```bash
sudo cp infra/demo/cmms-demo-reset.service infra/demo/cmms-demo-reset.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cmms-demo-reset.timer
systemctl list-timers cmms-demo-reset.timer
```

`scripts/demo_reset.sh` runs `docker compose down -v --remove-orphans`, brings every
long-running service back (everything except `seed`, so `db` and `app`, plus `edge` when the
public override is active) with `up -d --wait`, then runs the one-shot `seed` service with `run --rm` so its
exit code is the script's: the database volume is wiped and the seed rebuilds the synthetic
data and accounts. The site is down for roughly 1-2 minutes per reset.

### Cron alternative

```cron
*/30 * * * * /opt/shopfloor-cmms/scripts/demo_reset.sh >> /var/log/cmms-demo-reset.log 2>&1
```

## Public deployment behind Caddy

The layout for a host where Caddy already owns ports 80/443:

```
internet -> Caddy (host, TLS) -> 127.0.0.1:8000 -> edge (nginx, rate limits) -> app:8080
```

| File | Purpose |
|---|---|
| `docker-compose.public.yml` (repo root) | Override: `app` publishes no host port; adds `edge`, the only service bound to the host (`127.0.0.1:8000`). Needs Compose v2.24.4+ (`!override`). |
| `infra/demo/nginx.conf` | `edge` config: real client IP from `X-Forwarded-For`, rate limits, proxy to `app`. |
| `infra/demo/Caddyfile.snippet` | Site block to append to the host's `/etc/caddy/Caddyfile`. |

The override is selected by a `.env` in the checkout (git-ignored), which compose reads by
itself, so neither `scripts/demo_reset.sh` nor the systemd unit needs a flag:

```dotenv
COMPOSE_FILE=docker-compose.yml:docker-compose.public.yml
CMMS_PUBLIC_BASE_URL=https://work.example.com
```

Rate limits enforced by `edge` (per client IP, over-limit answers are HTTP 429):

- everything: 10 r/s, burst 40, at most 20 concurrent connections;
- `/app/login` (GET and POST alike): additionally 1 r/s, burst 5;
- `/health`: unlimited, for the compose healthcheck and Caddy.

**arm64:** the images (`postgres:17`, `nginx:1.27-alpine`, the Python base of the app
image) are multi-arch, so an Ampere A1 host works as is. The first `--build` of the app
image takes a few minutes on a 2-vCPU A1.

Order of operations:

1. Install Docker (Engine + Compose plugin v2.24.4+).
2. Clone the repo to `/opt/shopfloor-cmms`.
3. Write `/opt/shopfloor-cmms/.env` with the two lines above (real hostname).
4. Run `scripts/demo_reset.sh` once by hand (builds the image, starts the stack, seeds it).
5. Check `curl -s 127.0.0.1:8000/health`.
6. Enable the reset timer (section above).
7. Only once the DNS A record resolves (`getent hosts <host>`), append
   `infra/demo/Caddyfile.snippet` (with the real hostname) to `/etc/caddy/Caddyfile` and
   `sudo systemctl reload caddy`. Reloading before DNS resolves makes the ACME challenge
   fail on NXDOMAIN and Caddy backs off, delaying the certificate.

## What this does not do

- **Rate limiting only with the public override.** The plain compose stack has none; with
  `docker-compose.public.yml` the `edge` nginx limits each client IP to 10 r/s and
  `/app/login` to 1 r/s (see above).
- **No TLS.** Terminate HTTPS at the reverse proxy (Caddy, in the public layout).
- **Anyone can sign in as admin**, by design. Never point a demo-mode instance at real data.
- The app **refuses to start** if `CMMS_DEMO_MODE` is combined with `CMMS_APP_ENV=production`.
