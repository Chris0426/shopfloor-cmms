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

`scripts/demo_reset.sh` runs `docker compose down -v --remove-orphans`, brings `db` and
`app` back with `up -d --wait`, then runs the one-shot `seed` service with `run --rm` so its
exit code is the script's: the database volume is wiped and the seed rebuilds the synthetic
data and accounts. The site is down for roughly 1-2 minutes per reset.

### Cron alternative

```cron
*/30 * * * * /opt/shopfloor-cmms/scripts/demo_reset.sh >> /var/log/cmms-demo-reset.log 2>&1
```

## What this does not do

- **No rate limiting.** Put the host behind a reverse proxy that enforces limits.
- **No TLS.** Terminate HTTPS at the reverse proxy.
- **Anyone can sign in as admin**, by design. Never point a demo-mode instance at real data.
- The app **refuses to start** if `CMMS_DEMO_MODE` is combined with `CMMS_APP_ENV=production`.
