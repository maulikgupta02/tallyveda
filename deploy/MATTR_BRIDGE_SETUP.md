# Switching on the MATTR AI bridge, step by step

**What it does:** after every Tally sync, the backend (on Render, using Neon)
sends that company's books to n8n. n8n then updates the WhatsApp bot's
Postgres on the VM.

- Neon stays the original, the source of truth.
- The VM Postgres becomes an automatic copy of it. No more manual `pg_dump`.

```
Tally → connector → backend on Render → Neon (original)
                          │
                          └─ signed HTTPS → n8n workflow "Tally bridge (MATTR AI)"
                                               → apply_sync_batch() → VM Postgres (copy)
                                                                         ↑
                                       "Tally WhatsApp Bot v3" reads from here (unchanged)
```

Do the steps **in this order**. Get the n8n side ready first, so the backend
finds a working receiver the moment it is deployed.

---

## Step 0. Make the shared secret (on your Mac, 1 minute)

The backend signs every request with a password that only it and n8n know. You create it:

```bash
openssl rand -hex 32
```

It prints 64 letters and digits. That is **the secret**.

- Save it in a password manager.
- You will paste the same value in two places: n8n (step 3) and Render (step 6).
- Send it to the Render person through a password manager share, not email or WhatsApp.
- Never commit it to git.

## Step 1. Back up the VM database (5 minutes)

SSH into the VM, then:

```bash
docker exec postgres pg_dump -U postgres tally_data > ~/tally_data_before_bridge.sql
ls -lh ~/tally_data_before_bridge.sql      # should not be 0 bytes
```

If anything goes wrong later, this file restores the bot's data.

## Step 2. Install the bridge's database functions (5 minutes)

Copy `audit/n8n-reference/n8n_bridge.sql` from your Mac to the VM. On your Mac:

```bash
gcloud compute scp audit/n8n-reference/n8n_bridge.sql <vm-name>:~/ --zone <zone>
```

Any other copy method works too. Then, on the VM:

```bash
docker exec -i postgres psql -U postgres -d tally_data -v ON_ERROR_STOP=1 < ~/n8n_bridge.sql
```

It should end without any `ERROR` line. It is safe to run again. This adds
three tables and the functions n8n will call. It does not change or delete
any existing data.

Then check that the role works. `n8n_ingest` is the database user that the
**Tally ingest (secure)** workflow already uses:

```bash
docker exec -i postgres psql -U postgres -d tally_data -c "SET ROLE n8n_ingest; SELECT apply_sync_batch(false, '{}', '{}');"
```

You should see `{"error": "unauthorized", "status": 401}`. That is correct:
the role can call the function, and an unsigned request is refused.

## Step 3. Give n8n the secret and two settings (15 minutes, n8n restarts once)

These are **settings of the n8n program itself**, not of a workflow. They
can't be set inside the n8n editor. They are environment variables of the
Docker container n8n runs in:

| Variable | Value | Why |
|---|---|---|
| `MATTR_BRIDGE_SECRET` | the secret from step 0 | so the workflow can check the signature |
| `NODE_FUNCTION_ALLOW_BUILTIN` | `crypto,zlib` | lets the Code node compute the signature and unzip the data |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE` | `false` | lets the Code node read `MATTR_BRIDGE_SECRET` |

Your n8n was started with one long `docker run` command. To add settings, the
container is re-created with the same command plus these three `-e` lines.
Your workflows and credentials live in the `~/.n8n` folder on the VM, so they
are kept.

1. See how n8n is running now:
   ```bash
   docker ps --format '{{.Names}}  {{.Image}}  {{.Ports}}'
   docker inspect n8n --format '{{json .HostConfig.Binds}} {{json .HostConfig.NetworkMode}} {{json .HostConfig.PortBindings}} {{json .HostConfig.RestartPolicy}}'
   docker inspect n8n --format '{{range .Config.Env}}{{println .}}{{end}}'
   ```
   **Easiest:** send that output to Claude with every password or key value
   replaced by `***`, and ask for the exact new command.
2. The new command has this shape. Use your real values from step 1:
   ```bash
   docker stop n8n
   docker rename n8n n8n-old          # kept, so you can go back
   docker run -d --name n8n \
     --restart unless-stopped \
     --network n8n-net \
     -p 127.0.0.1:5678:5678 \
     -v ~/.n8n:/home/node/.n8n \
     -e ...every -e line you had before... \
     -e NODE_FUNCTION_ALLOW_BUILTIN=crypto,zlib \
     -e N8N_BLOCK_ENV_ACCESS_IN_NODE=false \
     -e MATTR_BRIDGE_SECRET=<the secret> \
     n8nio/n8n:<the same image tag as before>
   ```
3. Open `https://tally-bot.duckdns.org`, sign in, and check that both
   workflows are still there. Send the bot a WhatsApp message.
4. If something is wrong, go back:
   ```bash
   docker rm -f n8n && docker rename n8n-old n8n && docker start n8n
   ```
5. When all is well (a day later), delete the old container: `docker rm n8n-old`.

`N8N_BLOCK_ENV_ACCESS_IN_NODE=false` lets every workflow read every
environment variable of the container. That's acceptable while only you
edit workflows, but don't give other people editor access.

## Step 4. Import the new workflow (10 minutes)

1. In n8n: **Overview → Create workflow → ⋯ (top right) → Import from File**,
   and pick `audit/n8n-reference/Tally_bridge.json`.
2. Open the **Store and apply** node. Under *Credential*, pick the existing
   **Postgres n8n_ingest**, the same one **Tally ingest (secure)** uses. Close the node.
3. Click **Save**, then **Publish**.
4. Open the **Webhook** node and copy the **Production URL**. It should be
   `https://tally-bot.duckdns.org/webhook/mattr-bridge`. That is the URL for step 6.
5. Optional check from your Mac. An unsigned request should get a 401:
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" -X POST -H 'Content-Type: application/gzip' --data-binary 'x' https://tally-bot.duckdns.org/webhook/mattr-bridge
   ```
   You should see `401`. If you see `503`, the secret from step 3 isn't
   reaching n8n.

## Step 5. Get the code onto GitHub

The changes are on the `n8n-bridge` branch on your Mac. Push it and open a pull
request. Render deploys from GitHub, so the Render person can only deploy
what is there.

## Step 6. Send this to the person with Render access

> Please merge the `n8n-bridge` pull request. Then, in Render → the
> `tally-connector-backend` service → **Environment**, add these and deploy:
>
> | Key | Value |
> |---|---|
> | `TC_N8N_WEBHOOK_URL` | `https://tally-bot.duckdns.org/webhook/mattr-bridge` |
> | `TC_N8N_WEBHOOK_SECRET` | *(the secret, shared separately)* |
> | `TC_ALERT_EMAIL` | *(optional: an email address for delivery failures; only works if the `TC_SMTP_*` settings are already set there)* |
>
> On the first start the app adds two tables to Neon (`n8n_outbox`,
> `n8n_seq`). That's an addition only; no existing data is changed.

After the deploy, the backend sends every existing company by itself, three
at a time. It starts as soon as the site gets traffic, so opening the
dashboard once is enough. Each later sync sends that company again
automatically.

## Step 7. Check it worked (10 minutes)

On the VM:

```bash
docker exec -i postgres psql -U postgres -d tally_data -c \
  "SELECT application_id, max(batch_seq), max(applied_at) FROM applied_batches GROUP BY 1;"
docker exec -i postgres psql -U postgres -d tally_data -c \
  "SELECT application_id, batch_seq, status, error FROM bridge_inbox WHERE status IN ('stored','failed');"
```

The first query should list your companies with recent times. The second
should be empty.

Then ask the bot on WhatsApp: *"is Tally connected?"* The data date should be
today's or the last sync's, no longer 7 October.

In n8n, the **Executions** tab of the bridge workflow stays empty even when
everything works. That's deliberate: it doesn't save executions, to keep
financial data out of n8n. Use the two queries above instead.

## Step 8. Tidy up (after a week of it working)

- **Tally ingest (secure):** unpublish it (open it → the *Published* toggle).
  Nothing sends to it, because the connector only talks to the backend. The
  new bridge replaces it. Keep it unpublished for a month, then delete it.
- **Tally WhatsApp Bot v3:** no change needed. It reads the same tables, which are now kept fresh.
- **Leftovers from the old manual dump:** the dump also copied tables the bot
  never uses, including dashboard logins with password hashes. After taking a
  new backup (step 1), you can empty them:
  ```sql
  DELETE FROM leads; DELETE FROM audit_log; DELETE FROM connector_logs;
  DELETE FROM report_files; DELETE FROM reports;
  UPDATE wa_contacts SET user_id = NULL; DELETE FROM users;
  ```
- **A company deleted from Neon before the bridge existed** stays in the copy.
  Remove it by hand: `SELECT bridge_purge('<application id>');`

## If something goes wrong

| What you see | What it means | What to do |
|---|---|---|
| `bridge_rejects` has rows | wrong secret on one side | the two secrets must be identical; fix one, the backend retries |
| `bridge_inbox` rows with `status = 'failed'` | the copy couldn't be written | read the `error` column; the backend retries for 24 hours |
| curl in step 4 gives `503` | n8n doesn't have the secret | redo step 3 |
| Nothing arrives at all | Render env vars missing, or no traffic since the deploy | open the dashboard once; check the Render env vars |
