# Raspberry Pi Docker Compose Deployment

This runbook documents how to deploy the Finnish job portal on a Raspberry Pi
or other small ARM64 Linux host with Docker Compose.

It adapts the transferable operational pattern from
`../lorawan-server/docs/deployment/vps-setup/03-mapserver.md`,
`../lorawan-server/docs/deployment/vps-setup/06-security.md`,
`../lorawan-server/docs/deployment/vps-setup/journal-pi.md`,
`../lorawan-server/docs/deployment/cloudflare-tunnel-hosts.md`, and
`../lorawan-server/docs/deployment/journal-prod-app.md`: bind Docker ports to
localhost by default, use systemd to restart Compose after reboot, verify the
actual host serving the domain, and keep backups outside the Git checkout.

## Target Shape

The application already has the right Compose shape for a Pi:

- `web`: Next.js portal, host port `127.0.0.1:3080`
- `api`: FastAPI backend, host port `127.0.0.1:8008`
- `worker`: scheduled collectors, matching, embeddings, and LLM evaluation
- `db`: PostgreSQL + pgvector, private Docker volume

Default ports are intentionally not public:

```text
WEB_BIND=127.0.0.1
WEB_PORT=3080
API_BIND=127.0.0.1
API_PORT=8008
```

Keep these defaults for any deployment that uses Cloudflare Tunnel, Tailscale,
SSH forwarding, or a local reverse proxy on the Pi. Only set `WEB_BIND=0.0.0.0`
or `API_BIND=0.0.0.0` after a privacy/security review. This app contains a
private profile, recommendation history, and potentially hosted LLM request
metadata.

## Pi Prerequisites

Use a 64-bit Raspberry Pi OS or Ubuntu install. The current stack depends on
Docker images that should run on ARM64, but verify the final image set on the
target Pi before declaring it production-ready.

Minimum practical target:

- Raspberry Pi 4/5 or equivalent ARM64 host
- 4 GB RAM minimum; 8 GB preferred for Next.js builds and PostgreSQL
- SSD or reliable external storage for Docker volumes and backups
- Docker Engine with the Compose plugin
- Git and Make
- Outbound HTTPS access for job-source polling and optional hosted LLM calls

Recommended host packages:

```bash
sudo apt-get update
sudo apt-get install -y git make curl ca-certificates ufw fail2ban unattended-upgrades
```

Install Docker using the official Docker instructions for the Pi OS family,
then verify:

```bash
docker version
docker compose version
docker run --rm hello-world
```

## First Deploy

Clone the repo outside any web-served directory:

```bash
mkdir -p /home/petterip/git
cd /home/petterip/git
git clone <repo-url> job-search-agent
cd job-search-agent
```

Create the private env file from the example:

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` on the Pi:

```env
APP_VERSION=pi

WEB_BIND=127.0.0.1
WEB_PORT=3080
API_BIND=127.0.0.1
API_PORT=8008

POSTGRES_USER=jobsearchagent
POSTGRES_PASSWORD=<strong local password>
POSTGRES_DB=jobsearchagent
DATABASE_URL=postgresql+psycopg://jobsearchagent:<same password>@db:5432/jobsearchagent

INTERNAL_API_BASE_URL=http://api:8000
NEXT_PUBLIC_API_BASE_URL=https://<public-or-private-web-hostname>

LLM_PROVIDER=
OPENAI_API_KEY=
GEMINI_API_KEY=

COLLECTOR_DAILY_HOUR=16
COLLECTOR_DAILY_MINUTE=0
MATCHER_DAILY_HOUR=16
MATCHER_DAILY_MINUTE=0
```

Use `NEXT_PUBLIC_API_BASE_URL=http://<pi-lan-ip>:8008` only for a temporary LAN
test where the API is deliberately reachable from the browser. For the safer
default, expose only the web service through a same-host reverse proxy or tunnel
and keep the API bound to localhost; if browser-side API calls need a public URL,
that URL must route to the API intentionally.

Validate Compose without printing expanded secrets:

```bash
make docker-config
```

Build and start:

```bash
docker compose up --build -d
docker compose ps
```

Verify locally on the Pi:

```bash
curl -fsS http://127.0.0.1:8008/health
curl -fsSI http://127.0.0.1:3080
docker compose logs --tail=100 api worker web db
```

Load the private profile only from a trusted local path. Do not copy `profile/`
into Git or public backup targets:

```bash
docker compose exec api python -m app.profile /path/in/container/profile.yaml --name default
docker compose exec api python -m app.match --max-jobs 1000
```

Run a bounded collector smoke before enabling unattended operation:

```bash
docker compose exec api python -m app.collect duunitori --page-size 20 --max-pages 1 --force
make audit-db
```

## systemd Autostart

Create a systemd unit so the stack comes back after reboot:

```bash
sudo tee /etc/systemd/system/job-search-agent.service > /dev/null <<'EOF'
[Unit]
Description=Job Search Agent Docker Compose stack
After=docker.service network-online.target
Requires=docker.service
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/home/petterip/git/job-search-agent
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose stop
User=petterip
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now job-search-agent.service
sudo systemctl status job-search-agent.service
```

After a reboot:

```bash
sudo systemctl is-active job-search-agent.service
cd /home/petterip/git/job-search-agent
docker compose ps
curl -fsS http://127.0.0.1:8008/health
```

## Exposure Options

### Localhost Only

This is safest for maintenance and data collection. Access from a workstation
with SSH forwarding:

```bash
ssh -L 3080:127.0.0.1:3080 -L 8008:127.0.0.1:8008 petterip@<pi-host>
```

Then open `http://127.0.0.1:3080` locally.

### LAN Access

Use this only on a trusted LAN. Set:

```env
WEB_BIND=0.0.0.0
NEXT_PUBLIC_API_BASE_URL=http://<pi-lan-ip>:8008
```

Expose `API_BIND=0.0.0.0` only if browser-side calls cannot be routed through a
same-origin proxy. Mutation endpoints are local-private in the MVP, so LAN API
exposure should be temporary.

Restart and verify what is listening:

```bash
docker compose up -d
sudo ss -tlnp | grep -E '(:3080|:8008)'
```

### Cloudflare Tunnel

The lorawan runbooks' main lesson is to prove where each hostname actually
points before debugging app behavior. If using Cloudflare Tunnel, document the
hostname map for this project before relying on it.

Example tunnel targets:

```text
jobs.example.fi      -> http://localhost:3080
jobs-api.example.fi  -> http://localhost:8008
```

Verification pattern:

```bash
curl -fsSI http://127.0.0.1:3080
curl -fsSI https://jobs.example.fi
curl -fsS http://127.0.0.1:8008/health
curl -fsS https://jobs-api.example.fi/health
```

If a tunneled hostname returns stale behavior, compare the local and tunneled
responses first. Fix the tunnel target, local service, or edge cache before
changing application code.

## Deploy Updates

From the Pi:

```bash
cd /home/petterip/git/job-search-agent
git status --short
git fetch --prune
git log --oneline --decorate -5 --all
git pull --ff-only
make docker-config
docker compose build
docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:8008/health
make audit-db
```

For source changes, run `make test-regression` before editing source behavior
and again before deploy if source adapters or source docs changed.

Use a deploy note with:

- commit SHA deployed
- `.env` changes made, without secret values
- migration/build result
- health-check result
- backup file name, if a backup was taken

## Backups

The repo already provides PostgreSQL backup/restore targets:

```bash
make backup-db
make restore-db BACKUP=backups/jobsearchagent-YYYYMMDDTHHMMSSZ.dump
```

For Pi production, keep backups outside the Git checkout and preferably on a
separate disk. The Make target writes to `backups/` under the repo, so copy the
dump out immediately or replace the target later with a host backup directory.

Manual pattern:

```bash
mkdir -p /home/petterip/jobsearchagent-backups/manual
make backup-db
latest="$(ls -t backups/jobsearchagent-*.dump | head -1)"
cp "$latest" /home/petterip/jobsearchagent-backups/manual/
sha256sum /home/petterip/jobsearchagent-backups/manual/"$(basename "$latest")" \
  > /home/petterip/jobsearchagent-backups/manual/"$(basename "$latest")".sha256
```

Before any migration-heavy deploy, take a backup and verify that the dump file
is non-empty:

```bash
make backup-db
ls -lh backups/jobsearchagent-*.dump | tail -1
```

## Security Hardening

Carry over the lorawan Pi/VPS principle: Docker port bindings are the primary
control. UFW does not reliably protect ports that Docker publishes to
`0.0.0.0`, so prefer Compose bindings to `127.0.0.1`.

Baseline hardening:

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp
sudo ufw --force enable
sudo ufw status verbose
```

SSH hardening:

```bash
sudo tee /etc/ssh/sshd_config.d/99-hardening.conf > /dev/null <<'EOF'
PermitRootLogin no
PasswordAuthentication no
KbdInteractiveAuthentication no
MaxAuthTries 3
EOF
sudo sshd -t
sudo systemctl reload ssh
```

Before enabling fail2ban, make sure the IPs used for administration are in
`ignoreip` to avoid self-lockout:

```bash
sudo tee /etc/fail2ban/jail.local > /dev/null <<'EOF'
[DEFAULT]
ignoreip = 127.0.0.0/8 ::1 <your-admin-public-ip>
bantime = 30m
findtime = 60m
maxretry = 10

[sshd]
enabled = true
EOF
sudo systemctl enable --now fail2ban
sudo fail2ban-client status sshd
sudo fail2ban-client get sshd ignoreip
```

Enable unattended security upgrades:

```bash
sudo dpkg-reconfigure -f noninteractive unattended-upgrades
```

## Operational Checks

Safe read-only checks:

```bash
cd /home/petterip/git/job-search-agent
git status --short
git log -1 --oneline
docker compose ps
docker compose logs --tail=100 api worker web db
df -h / /home /var/lib/docker
docker system df
curl -fsS http://127.0.0.1:8008/health
make audit-db
```

Collector and matcher checks:

```bash
docker compose exec api python -m app.collect duunitori --page-size 20 --max-pages 1 --force
docker compose exec api python -m app.match --max-jobs 1000
docker compose exec api python -m app.audit
```

If hosted LLM evaluation is enabled, monitor quota failures and cooldowns:

```bash
docker compose logs --tail=200 worker | grep -Ei 'llm|quota|cooldown|provider'
```

The worker uses `Europe/Helsinki` time. By default all enabled source searches
and the matching/LLM pass are scheduled once per day at 16:00. Change
`COLLECTOR_DAILY_*` and `MATCHER_DAILY_*` in `.env` if the Pi should use a
different quiet-hour window.

## Rollback

For code-only failures:

```bash
cd /home/petterip/git/job-search-agent
git log --oneline -5
git checkout <previous-good-sha>
make docker-config
docker compose build
docker compose up -d
curl -fsS http://127.0.0.1:8008/health
```

For database failures, restore only after confirming the target backup:

```bash
make restore-db BACKUP=/path/to/jobsearchagent-YYYYMMDDTHHMMSSZ.dump
docker compose up -d
make audit-db
```

Record the restore in deploy notes. A restore can discard newer collector,
recommendation, and feedback rows.

## Open Items Before Real Production

- Decide the real Pi hostname, SSH alias, and repo path.
- Decide whether access is SSH-forwarded, LAN-only, Cloudflare Tunnel, or a
  local HTTPS reverse proxy.
- Add the project's actual hostname map once chosen.
- Move PostgreSQL backups to a path outside the checkout by default.
- Verify every Docker image on the target ARM64 Pi.
- Decide whether hosted LLM calls are acceptable for this deployment.
- Add authentication before exposing mutation endpoints beyond localhost.
