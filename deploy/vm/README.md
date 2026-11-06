# Running the backend on the n8n VM

This runs the TallyVeda backend (dashboard, admin, connector API) in Docker on
the same GCP VM as n8n, behind the Caddy that already serves
`tally-bot.duckdns.org`. Render keeps working from `render.yaml`. Nothing here
changes it.

```
internet ─ Caddy (host, TLS) ─┬─ 127.0.0.1:5678  n8n          (tally-bot.duckdns.org)
                              └─ 127.0.0.1:8000  tallyveda    (tallyveda.duckdns.org)
                                       │
                                       └─ Neon (unchanged system of record)
```

## Before you start

- **Memory.** n8n, Postgres and this backend together need about 1.5–2 GB of RAM.
  Check with `free -h`. The container is capped at 512 MB.
- **Backups.** Take a VM disk snapshot first (AUDIT F-H1).
- **Connectors already installed keep talking to Render.** Each one saved the
  server address it was set up with. So run both side by side at first (see
  "Cutting over"), and don't switch Render off until every company uses the new address.

## 1. A hostname

Create a second DuckDNS subdomain (for example `tallyveda`) pointing at the VM's
external IP, the same way `tally-bot` was set up.

## 2. Code and settings

```bash
git clone https://github.com/SoumitraRai/tallyveda.git ~/tallyveda
cd ~/tallyveda && git checkout n8n-bridge     # or main once merged
cd deploy/vm
cp tallyveda.env.example tallyveda.env && chmod 600 tallyveda.env
nano tallyveda.env
```

Fill it in with the values from Render's **Environment** tab. Use the same
`DATABASE_URL` (Neon), the same admin settings, and SMTP if you use it.

## 3. Start it

```bash
docker network ls | grep n8n-net      # must exist; it is the network n8n uses
docker compose up -d --build
docker compose ps                     # STATUS should become "healthy"
curl -s 127.0.0.1:8000/healthz
docker compose logs -f --tail=50
```

On its first start against Neon, the app adds its new tables and columns, as on
every deploy. This is the same migration Render runs when you deploy the branch.

## 4. Caddy

Add a block to the Caddyfile (often `/etc/caddy/Caddyfile`) and reload:

```
tallyveda.duckdns.org {
    encode gzip
    request_body {
        max_size 300MB          # TC_MAX_UPLOAD_MB: one-shot uploads from older connectors
    }
    reverse_proxy 127.0.0.1:8000
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile && sudo systemctl reload caddy
```

Open `https://tallyveda.duckdns.org/admin` and sign in with your existing platform login.

## 5. The MATTR AI bridge on this server

Set `TC_N8N_WEBHOOK_URL` and `TC_N8N_WEBHOOK_SECRET` in `tallyveda.env`, then run
`docker compose up -d`. Set them on **one** server only, either Render or the VM.
Two servers sending the same outbox is harmless (n8n ignores repeats), but it
doubles the traffic.

Commands run inside the container:

```bash
docker exec -it tallyveda python -m app.manage mattr-forward <application_id> on
docker exec -it tallyveda python -m app.manage n8n-deliver
docker exec -i postgres psql -U postgres -d tally_data -Atc 'SELECT bridge_reconcile_all()' > /tmp/n8n.json
docker cp /tmp/n8n.json tallyveda:/tmp/n8n.json
docker exec -it tallyveda python -m app.manage n8n-reconcile --compare /tmp/n8n.json
```

Unlike on Render's free plan, this server never sleeps, so retries happen on time.

## Cutting over (optional, later)

1. Run both: Render for the connectors already installed, the VM for the
   dashboard. Both use Neon, so they show the same data.
2. Build new connectors with the new address:
   `SERVER=https://tallyveda.duckdns.org VERSION=... connector/build.sh`. Set
   `TC_CONNECTOR_URL` (or the bundled zip) on both servers to that build. New
   shares then use the VM.
3. Re-link the existing companies with the new connector (a new code from the
   dashboard). When no company has checked in to Render for a while, turn Render off.

## Updating and rolling back

```bash
cd ~/tallyveda && git pull && cd deploy/vm && docker compose up -d --build
```

To roll back, check out the previous commit and run `docker compose up -d --build` again.
Neon is untouched by a rollback: the new tables and columns are additive, and
older code ignores them.
