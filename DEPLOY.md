# ReCharge Alaska Portal v3 — VPS Deployment Runbook

## Prerequisites (one-time)
- Hostinger VPS: Ubuntu 24.04, Docker + Docker Compose installed
- Caddy running as reverse proxy (managing v2 already)
- `rca_net` Docker network already exists (created by v2 stack)
- DNS: `www.rechargealaska.net` → VPS IP (verify before deploying)

---

## 1. SSH into VPS
```bash
ssh user@<VPS_IP>
```

---

## 2. Clone the repo
```bash
cd /opt
git clone https://github.com/<your-org>/recharge-portal-v3.git rca-v3
cd rca-v3
```

---

## 3. Create the .env file (backend secrets)
```bash
cp .env.example .env
nano .env
```

Fill in:
```
SUPABASE_URL=https://tgnolprmusnapfuydubt.supabase.co
SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9...
SUPABASE_SERVICE_ROLE_KEY=<GET FROM SUPABASE DASHBOARD → Settings → API>
DATABASE_URL=postgresql://postgres.<ref>:<password>@aws-1-us-east-2.pooler.supabase.com:5432/postgres
SMTP_HOST=smtp.office365.com
SMTP_PORT=587
SMTP_USER=info@rechargealaska.net
SMTP_PASSWORD=<M365 app password>
ALERT_EMAIL_TO=info@rechargealaska.net
ALERT_EMAIL_FROM=info@rechargealaska.net
APP_MODE=web
SECRET_KEY=<run: openssl rand -hex 32>
ALLOWED_ORIGINS=https://www.rechargealaska.net
# DEV_BYPASS_AUTH must NOT be set (defaults false = real auth enabled)
```

---

## 4. Build and start the containers
```bash
docker compose build \
  --build-arg VITE_SUPABASE_URL=https://tgnolprmusnapfuydubt.supabase.co \
  --build-arg VITE_SUPABASE_ANON_KEY=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9... \
  --build-arg VITE_API_BASE=

docker compose up -d
docker compose ps   # confirm both containers are "running"
docker compose logs --tail=30 rca_api_v3   # confirm API started cleanly
```

---

## 5. Copy built frontend to where Caddy can serve it
```bash
# The web container writes dist → the named volume rca_web_v3_dist.
# Create a symlink or copy to where Caddy expects it:
docker run --rm \
  -v rca_web_v3_dist:/src \
  -v /srv/app:/dst \
  alpine cp -r /src/. /dst/
```

> Or, simpler: mount the volume directly in Caddy's Docker container if
> Caddy is also running in Docker.  Adjust to match your existing v2 setup.

---

## 6. Update the VPS Caddyfile
Merge the v3 block from `Caddyfile` (in this repo) into `/etc/caddy/Caddyfile`
(or wherever Caddy reads its config on your VPS):

```
www.rechargealaska.net {
    handle /api/* {
        reverse_proxy rca_api_v3:8000 {
            header_up Host {host}
            flush_interval -1
        }
    }

    handle /app* {
        root * /srv/app
        try_files {path} /index.html
        file_server
    }

    handle / {
        redir /app permanent
    }
}
```

Then reload Caddy:
```bash
caddy reload --config /etc/caddy/Caddyfile
# or if Caddy is in Docker:
docker exec caddy caddy reload --config /etc/caddy/Caddyfile
```

---

## 7. Smoke test
- Visit https://www.rechargealaska.net/app — should show login
- Log in as kris.hall@rechargealaska.net — all tabs visible including Admin
- Log in as another portal user — Admin tab hidden
- Trigger a test alert (or check fired_alerts table has recent rows)

---

## 8. Redirect v2 → v3 (when ready)
Add this block to the VPS Caddyfile (keep v2 containers running until confident):
```
dashboard.rechargealaska.net {
    redir https://www.rechargealaska.net/app{uri} 301
}
```
Reload Caddy. Users hitting the old URL are silently redirected.

---

## 9. Decommission v2 (final step — no rush)
```bash
cd /opt/rca-v2   # wherever v2 docker-compose lives
docker compose down
```
Leave the Caddyfile redirect block in place permanently (zero cost).

---

## Updating v3 after code changes
```bash
cd /opt/rca-v3
git pull
docker compose build --build-arg VITE_SUPABASE_URL=... --build-arg VITE_SUPABASE_ANON_KEY=...
docker compose up -d
# Re-copy frontend dist if changed (step 5 above)
```
