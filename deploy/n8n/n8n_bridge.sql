-- =====================================================================
-- MATTR AI bridge, receiving side: the backend pushes each company's
-- APPLIED books (backend/app/n8n.py); this replaces that company's rows.
-- The backend (Neon) is the system of record. This database is a projection
-- and never computes books itself. Supersedes the apply logic of
-- tally_ingest.sql (AUDIT F-J3); its tables can stay until Phase 3.
--
-- Run after whatsapp_bot.sql and access_v1.sql, as postgres:
--   docker exec -i postgres psql -U postgres -d tally_data -v ON_ERROR_STOP=1 < n8n_bridge.sql
-- Safe to re-run.
--
-- Called by the "Tally bridge" workflow (Tally_bridge.json), which checks the
-- HMAC signature and timestamp on the raw body and then runs, as n8n_ingest:
--   SELECT apply_sync_batch(<sig_ok>, <headers jsonb>, <part jsonb>);
-- It answers {status: 200|202|400|401|409|503, ...}:
--   200 applied, or a duplicate (batch_seq <= last applied): nothing changes
--   202 part stored, waiting for the other parts of the batch
--   400 malformed     401 bad signature (nothing stored)
--   409 a 'status' batch that is not last+1: the backend then sends a snapshot
--   503 applying failed: the parts stay in bridge_inbox and a resend retries
--
-- Batch kinds (all parts of one batch_seq are applied together, in ONE
-- transaction, whatever order they arrived in):
--   snapshot  applications (listed columns only), books, book_vouchers,
--             sync_sessions replaced. Self-contained, so any snapshot newer
--             than the last applied batch is accepted; older ones are ignored.
--   status    Tally connection columns of applications; must be last+1.
--   purge     the company's rows, its WhatsApp access, sessions, invites and
--             the chat memory of every phone that had access. Self-contained.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. TABLES
-- ---------------------------------------------------------------------

-- Every verified part, stored before anything is applied (dead-letter queue).
-- The body is dropped once its batch is applied or superseded.
CREATE TABLE IF NOT EXISTS bridge_inbox (
    id              bigserial   PRIMARY KEY,
    received_at     timestamptz NOT NULL DEFAULT now(),
    application_id  text        NOT NULL,
    batch_seq       bigint      NOT NULL,
    kind            text        NOT NULL CHECK (kind IN ('snapshot', 'status', 'purge')),
    part            int         NOT NULL,
    of_parts        int         NOT NULL,
    idempotency_key text,
    body            jsonb,
    body_sha256     text        NOT NULL,
    status          text        NOT NULL DEFAULT 'stored'
                    CHECK (status IN ('stored', 'applied', 'ignored', 'failed')),
    error           text,
    applied_at      timestamptz,
    UNIQUE (application_id, batch_seq, part)
);
CREATE INDEX IF NOT EXISTS bridge_inbox_open ON bridge_inbox (status, id) WHERE status <> 'applied';

-- One row per applied batch. No foreign key: it must outlive a purge, so a
-- replayed old batch is still recognised as a duplicate.
CREATE TABLE IF NOT EXISTS applied_batches (
    application_id text        NOT NULL,
    batch_seq      bigint      NOT NULL,
    kind           text        NOT NULL,
    hash           text        NOT NULL,   -- sha256 over the parts' sha256, in part order
    applied_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (application_id, batch_seq)
);

-- Calls with a bad signature. No payload is stored for these.
CREATE TABLE IF NOT EXISTS bridge_rejects (
    id             bigserial   PRIMARY KEY,
    at             timestamptz NOT NULL DEFAULT now(),
    application_id text,
    reason         text        NOT NULL
);

-- ---------------------------------------------------------------------
-- 2. APPLY (internal)
-- ---------------------------------------------------------------------

-- The applications columns the backend sends. Nothing else in the row is touched.
CREATE OR REPLACE FUNCTION _bridge_upsert_application(p_app text, p_row jsonb, p_insert boolean)
RETURNS void LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE
  r applications;
BEGIN
  IF p_row IS NULL OR p_row->>'id' IS DISTINCT FROM p_app THEN
    RAISE EXCEPTION 'application row missing or for another company';
  END IF;
  r := jsonb_populate_record(NULL::applications, p_row);
  IF p_insert THEN
    INSERT INTO applications (id, tenant_id, company_name, applicant_name, status, months, created_at,
                              deletion_requested_at, raw_purged_at, connector_version,
                              tally_ok_at, tally_down_since, tally_error)
    VALUES (r.id, COALESCE(r.tenant_id, ''), r.company_name, r.applicant_name, r.status, COALESCE(r.months, 0),
            r.created_at, r.deletion_requested_at, r.raw_purged_at, r.connector_version,
            r.tally_ok_at, r.tally_down_since, r.tally_error)
    ON CONFLICT (id) DO UPDATE SET
      tenant_id = EXCLUDED.tenant_id, company_name = EXCLUDED.company_name,
      applicant_name = EXCLUDED.applicant_name, status = EXCLUDED.status, months = EXCLUDED.months,
      created_at = EXCLUDED.created_at, deletion_requested_at = EXCLUDED.deletion_requested_at,
      raw_purged_at = EXCLUDED.raw_purged_at, connector_version = EXCLUDED.connector_version,
      tally_ok_at = EXCLUDED.tally_ok_at, tally_down_since = EXCLUDED.tally_down_since,
      tally_error = EXCLUDED.tally_error;
  ELSE
    -- A status update never brings back a company that was purged here.
    UPDATE applications SET
      company_name = r.company_name, applicant_name = r.applicant_name, status = r.status,
      deletion_requested_at = r.deletion_requested_at, raw_purged_at = r.raw_purged_at,
      connector_version = r.connector_version, tally_ok_at = r.tally_ok_at,
      tally_down_since = r.tally_down_since, tally_error = r.tally_error
    WHERE id = p_app;
  END IF;
END $$;

CREATE OR REPLACE FUNCTION _bridge_apply_snapshot(p_app text, p_seq bigint)
RETURNS void LANGUAGE plpgsql SET search_path = public, pg_temp AS $$
DECLARE
  v_head jsonb;
  v_expected int;
  v_got int;
  b books;
BEGIN
  SELECT body INTO v_head FROM bridge_inbox
   WHERE application_id = p_app AND batch_seq = p_seq AND part = 1;

  PERFORM _bridge_upsert_application(p_app, v_head->'application', true);

  b := jsonb_populate_record(NULL::books, v_head->'book');
  IF b.application_id IS DISTINCT FROM p_app OR b.state IS NULL THEN
    RAISE EXCEPTION 'snapshot without a book for this company';
  END IF;
  DELETE FROM books WHERE application_id = p_app;
  INSERT INTO books (application_id, state, max_alter_id, last_full_at, updated_at)
  VALUES (p_app, b.state, COALESCE(b.max_alter_id, 0), b.last_full_at, b.updated_at);

  DELETE FROM book_vouchers WHERE application_id = p_app;
  INSERT INTO book_vouchers (application_id, guid, vdate, alter_id, ledgers, body)
  SELECT p_app, v.guid, v.vdate, COALESCE(v.alter_id, 0), v.ledgers, v.body
    FROM bridge_inbox i,
         jsonb_to_recordset(i.body->'vouchers') AS v(guid text, vdate text, alter_id bigint, ledgers text, body text)
   WHERE i.application_id = p_app AND i.batch_seq = p_seq;
  GET DIAGNOSTICS v_got = ROW_COUNT;
  v_expected := (v_head->>'voucher_count')::int;
  IF v_expected IS DISTINCT FROM v_got THEN
    RAISE EXCEPTION 'snapshot has % vouchers, expected %', v_got, v_expected;
  END IF;

  DELETE FROM sync_sessions WHERE application_id = p_app;
  IF jsonb_typeof(v_head->'sync_session') = 'object' THEN
    INSERT INTO sync_sessions (id, application_id, mode, status, period_from, period_to, months_done, staged,
                               created_at, updated_at)
    SELECT s.id, p_app, s.mode, s.status, s.period_from, s.period_to, COALESCE(s.months_done, '[]'), '{}',
           s.created_at, s.updated_at
      FROM jsonb_populate_record(NULL::sync_sessions, v_head->'sync_session') s;
  END IF;
END $$;

-- Remove a company completely. Also callable by hand: SELECT bridge_purge('<id>');
CREATE OR REPLACE FUNCTION bridge_purge(p_app text)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_phone text;
  t text;
BEGIN
  -- Chat memory of everyone who could see this company (bot_forget: access_v1.sql).
  IF to_regprocedure('bot_forget(text)') IS NOT NULL THEN
    FOR v_phone IN SELECT DISTINCT phone FROM wa_company_access WHERE application_id = p_app LOOP
      PERFORM bot_forget(v_phone);
    END LOOP;
  END IF;
  DELETE FROM wa_sessions WHERE active_application_id = p_app;
  DELETE FROM book_vouchers WHERE application_id = p_app;
  DELETE FROM books         WHERE application_id = p_app;
  DELETE FROM sync_sessions WHERE application_id = p_app;
  -- Tables the one-off pg_dump or the old connector-event ingest left behind, where present.
  FOREACH t IN ARRAY ARRAY['wa_company_access', 'wa_invites', 'tally_present', 'tally_inbox', 'tally_ingest_keys',
                           'report_files', 'reports', 'connector_logs', 'users'] LOOP
    IF to_regclass('public.' || t) IS NOT NULL THEN
      EXECUTE format('DELETE FROM public.%I WHERE application_id = $1', t) USING p_app;
    END IF;
  END LOOP;
  DELETE FROM applications WHERE id = p_app;
  UPDATE bridge_inbox SET body = NULL, status = CASE WHEN status = 'applied' THEN status ELSE 'ignored' END
   WHERE application_id = p_app AND body IS NOT NULL;
END $$;

-- ---------------------------------------------------------------------
-- 3. ENTRY POINT (the only thing n8n_ingest may call)
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION apply_sync_batch(p_sig_ok boolean, p_headers jsonb, p_body jsonb)
RETURNS jsonb LANGUAGE plpgsql SECURITY DEFINER SET search_path = public, pg_temp AS $$
DECLARE
  v_app  text;
  v_kind text;
  v_seq  bigint;
  v_part int;
  v_of   int;
  v_last bigint;
  v_have int;
  v_hash text;
  v_key  text := p_headers->>'x-idempotency-key';
BEGIN
  IF NOT COALESCE(p_sig_ok, false) THEN
    INSERT INTO bridge_rejects (application_id, reason)
    VALUES (left(p_headers->>'x-application-id', 64), 'bad or missing signature');
    RETURN jsonb_build_object('status', 401, 'error', 'unauthorized');
  END IF;

  IF jsonb_typeof(p_body) IS DISTINCT FROM 'object'
     OR jsonb_typeof(p_body->'batch_seq') IS DISTINCT FROM 'number'
     OR jsonb_typeof(p_body->'part') IS DISTINCT FROM 'number'
     OR jsonb_typeof(p_body->'of') IS DISTINCT FROM 'number' THEN
    RETURN jsonb_build_object('status', 400, 'error', 'malformed');
  END IF;
  v_app  := p_body->>'application_id';
  v_kind := p_body->>'kind';
  v_seq  := (p_body->>'batch_seq')::bigint;
  v_part := (p_body->>'part')::int;
  v_of   := (p_body->>'of')::int;
  IF v_app IS NULL OR v_app !~ '^[A-Za-z0-9_-]{1,64}$'
     OR v_kind NOT IN ('snapshot', 'status', 'purge')
     OR v_seq < 1 OR v_of < 1 OR v_of > 1000 OR v_part < 1 OR v_part > v_of
     OR (v_kind <> 'snapshot' AND v_of <> 1)
     OR (v_key IS NOT NULL AND v_key <> v_app || ':' || v_seq || ':' || v_part) THEN
    RETURN jsonb_build_object('status', 400, 'error', 'malformed');
  END IF;

  -- One company at a time; other companies are not blocked.
  PERFORM pg_advisory_xact_lock(hashtextextended('mattr-bridge:' || v_app, 0));

  SELECT COALESCE(max(batch_seq), 0) INTO v_last FROM applied_batches WHERE application_id = v_app;
  IF v_seq <= v_last THEN
    RETURN jsonb_build_object('status', 200, 'result', 'duplicate', 'applied_seq', v_last);
  END IF;
  IF v_kind = 'status' AND v_seq <> v_last + 1 THEN
    RETURN jsonb_build_object('status', 409, 'error', 'out_of_order', 'expected_seq', v_last + 1);
  END IF;

  INSERT INTO bridge_inbox (application_id, batch_seq, kind, part, of_parts, idempotency_key, body, body_sha256)
  VALUES (v_app, v_seq, v_kind, v_part, v_of, v_key, p_body,
          encode(sha256(convert_to(p_body::text, 'UTF8')), 'hex'))
  ON CONFLICT (application_id, batch_seq, part) DO UPDATE
    SET body = EXCLUDED.body, body_sha256 = EXCLUDED.body_sha256, status = 'stored', error = NULL
    WHERE bridge_inbox.status = 'failed';   -- a resend after a failed apply retries it

  SELECT count(*) INTO v_have FROM bridge_inbox
   WHERE application_id = v_app AND batch_seq = v_seq AND status = 'stored';
  IF v_have < v_of THEN
    RETURN jsonb_build_object('status', 202, 'result', 'stored', 'parts', v_have, 'of', v_of);
  END IF;

  BEGIN
    IF v_kind = 'snapshot' THEN
      PERFORM _bridge_apply_snapshot(v_app, v_seq);
    ELSIF v_kind = 'status' THEN
      PERFORM _bridge_upsert_application(v_app, p_body->'application', false);
    ELSE
      PERFORM bridge_purge(v_app);
    END IF;

    SELECT encode(sha256(convert_to(string_agg(body_sha256, '' ORDER BY part), 'UTF8')), 'hex') INTO v_hash
      FROM bridge_inbox WHERE application_id = v_app AND batch_seq = v_seq;
    INSERT INTO applied_batches (application_id, batch_seq, kind, hash) VALUES (v_app, v_seq, v_kind, v_hash);
    UPDATE bridge_inbox SET status = 'applied', body = NULL, applied_at = now()
     WHERE application_id = v_app AND batch_seq = v_seq;
    -- Unfinished older batches can never apply now.
    UPDATE bridge_inbox SET status = 'ignored', body = NULL
     WHERE application_id = v_app AND batch_seq < v_seq AND status IN ('stored', 'failed');
  EXCEPTION WHEN OTHERS THEN
    UPDATE bridge_inbox SET status = 'failed', error = left(SQLERRM, 300)
     WHERE application_id = v_app AND batch_seq = v_seq;
    RETURN jsonb_build_object('status', 503, 'error', 'apply_failed', 'retry', true);
  END;

  RETURN jsonb_build_object('status', 200, 'result', 'applied', 'applied_seq', v_seq, 'kind', v_kind);
END $$;

-- ---------------------------------------------------------------------
-- 4. RECONCILIATION (owner only; compare with `python -m app.manage n8n-reconcile`)
-- Per company: voucher count, debits/credits by month (amounts are
-- debit-positive), max AlterId, SHA-256 of sorted "ledger<TAB>closing" lines.
-- Both sides round to 2 decimals the same way, so equal books give equal JSON.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION bridge_reconcile(p_app text)
RETURNS jsonb LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  WITH v AS (
    SELECT vdate, alter_id, body::jsonb AS b FROM book_vouchers WHERE application_id = p_app
  ), e AS (
    SELECT left(v.vdate, 7) AS m, COALESCE((x->>'amount')::numeric, 0) AS a
      FROM v LEFT JOIN LATERAL jsonb_array_elements(COALESCE(v.b->'entries', '[]'::jsonb)) x ON true
  ), months AS (
    SELECT m, COALESCE(sum(a) FILTER (WHERE a > 0), 0) AS dr, COALESCE(-sum(a) FILTER (WHERE a < 0), 0) AS cr
      FROM e GROUP BY m
  ), led AS (
    SELECT l->>'name' AS name, round(COALESCE((l->>'closing_balance')::numeric, 0), 2)::text AS closing
      FROM books bk, jsonb_array_elements(COALESCE(bk.state::jsonb->'ledgers', '[]'::jsonb)) l
     WHERE bk.application_id = p_app
  )
  SELECT jsonb_build_object(
    'application_id', p_app,
    'vouchers', (SELECT count(*) FROM v),
    'max_alter_id', GREATEST(COALESCE((SELECT max(alter_id) FROM v), 0),
                             COALESCE((SELECT max_alter_id FROM books WHERE application_id = p_app), 0)),
    'months', COALESCE((SELECT jsonb_object_agg(m, jsonb_build_object('dr', round(dr, 2)::text, 'cr', round(cr, 2)::text))
                          FROM months), '{}'::jsonb),
    'closing_sha256', encode(sha256(convert_to(COALESCE(
        (SELECT string_agg(name || E'\t' || closing || E'\n', '' ORDER BY name COLLATE "C") FROM led), ''), 'UTF8')), 'hex')
  );
$$;

CREATE OR REPLACE FUNCTION bridge_reconcile_all()
RETURNS jsonb LANGUAGE sql STABLE SET search_path = public, pg_temp AS $$
  SELECT COALESCE(jsonb_agg(bridge_reconcile(application_id) ORDER BY application_id), '[]'::jsonb) FROM books;
$$;

-- ---------------------------------------------------------------------
-- 5. LOCK DOWN: n8n_ingest may only call apply_sync_batch()
-- ---------------------------------------------------------------------
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'n8n_ingest') THEN
    CREATE ROLE n8n_ingest LOGIN PASSWORD 'CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD';
  END IF;
END $$;

GRANT CONNECT ON DATABASE tally_data TO n8n_ingest;
GRANT USAGE ON SCHEMA public TO n8n_ingest;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM n8n_ingest;

REVOKE EXECUTE ON FUNCTION _bridge_upsert_application(text, jsonb, boolean) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION _bridge_apply_snapshot(text, bigint)             FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION bridge_purge(text)                               FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION bridge_reconcile(text)                           FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION bridge_reconcile_all()                           FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION apply_sync_batch(boolean, jsonb, jsonb)          FROM PUBLIC;
GRANT  EXECUTE ON FUNCTION apply_sync_batch(boolean, jsonb, jsonb)          TO n8n_ingest;

-- =====================================================================
-- OPERATIONS CHEAT SHEET (run as postgres)
--
-- Set the real password (if the role was just created here):
--   ALTER ROLE n8n_ingest PASSWORD 'a-long-random-password';
--
-- What is the newest batch applied per company?
--   SELECT application_id, max(batch_seq), max(applied_at) FROM applied_batches GROUP BY 1;
-- What is stuck?
--   SELECT application_id, batch_seq, kind, part, of_parts, status, error, received_at
--   FROM bridge_inbox WHERE status IN ('stored', 'failed') ORDER BY id;
--   (fix the cause, then on the backend: python -m app.manage n8n-resync <id>)
-- Bad signatures:
--   SELECT application_id, count(*), max(at) FROM bridge_rejects
--   WHERE at > now() - interval '1 day' GROUP BY 1 ORDER BY 2 DESC;
--
-- Reconcile (save the output, then on the backend:
--   python -m app.manage n8n-reconcile --compare n8n.json):
--   docker exec -i postgres psql -U postgres -d tally_data -Atc 'SELECT bridge_reconcile_all()' > n8n.json
--
-- Housekeeping (e.g. weekly):
--   DELETE FROM bridge_inbox WHERE status IN ('applied', 'ignored') AND received_at < now() - interval '30 days';
--   DELETE FROM bridge_rejects WHERE at < now() - interval '30 days';
--
-- Verify the lockdown:
--   SET ROLE n8n_ingest;
--   SELECT * FROM book_vouchers LIMIT 1;                       -- must fail: permission denied
--   SELECT apply_sync_batch(false, '{}', '{}');                -- returns status 401
--   RESET ROLE;
-- =====================================================================
