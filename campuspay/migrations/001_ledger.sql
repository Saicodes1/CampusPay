CREATE TABLE accounts (
  id text PRIMARY KEY, owner_name text NOT NULL,
  kind text NOT NULL CHECK (kind IN ('STUDENT','MERCHANT','FUNDING','SETTLED')),
  normal_side text NOT NULL CHECK (normal_side IN ('DEBIT','CREDIT')),
  currency text NOT NULL DEFAULT 'USD' CHECK (currency = 'USD'),
  balance bigint NOT NULL DEFAULT 0 CHECK (balance >= 0),
  reserved bigint NOT NULL DEFAULT 0 CHECK (reserved >= 0 AND reserved <= balance)
);
CREATE TABLE journals (
  id uuid PRIMARY KEY, reference text NOT NULL UNIQUE, kind text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE entries (
  id bigserial PRIMARY KEY, journal_id uuid NOT NULL REFERENCES journals(id),
  account_id text NOT NULL REFERENCES accounts(id),
  side text NOT NULL CHECK (side IN ('DEBIT','CREDIT')),
  amount bigint NOT NULL CHECK (amount > 0)
);
CREATE INDEX entries_account ON entries(account_id, id);
CREATE FUNCTION immutable_ledger() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'ledger is append only'; END $$;
CREATE TRIGGER immutable_entries BEFORE UPDATE OR DELETE ON entries FOR EACH ROW EXECUTE FUNCTION immutable_ledger();
CREATE TRIGGER immutable_journals BEFORE UPDATE OR DELETE ON journals FOR EACH ROW EXECUTE FUNCTION immutable_ledger();
CREATE FUNCTION check_journal() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE jid uuid; total numeric; count_lines integer;
BEGIN
  IF TG_TABLE_NAME = 'journals' THEN jid := NEW.id; ELSE jid := NEW.journal_id; END IF;
  SELECT COALESCE(SUM(CASE WHEN side = 'DEBIT' THEN amount::numeric ELSE -amount::numeric END),0), COUNT(*)
    INTO total, count_lines FROM entries WHERE journal_id = jid;
  IF total <> 0 OR count_lines < 2 THEN RAISE EXCEPTION 'unbalanced journal %', jid; END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER balanced_journal AFTER INSERT ON journals DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_journal();
CREATE CONSTRAINT TRIGGER balanced_entries AFTER INSERT ON entries DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_journal();
CREATE FUNCTION check_projection() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE aid text; expected numeric; actual bigint;
BEGIN
  IF TG_TABLE_NAME = 'accounts' THEN aid := NEW.id; ELSE aid := NEW.account_id; END IF;
  SELECT a.balance, COALESCE(SUM(CASE WHEN e.side = a.normal_side THEN e.amount::numeric ELSE -e.amount::numeric END),0)
    INTO actual, expected FROM accounts a LEFT JOIN entries e ON e.account_id = a.id WHERE a.id = aid GROUP BY a.id;
  IF actual <> expected THEN RAISE EXCEPTION 'balance projection mismatch %', aid; END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER account_projection AFTER INSERT OR UPDATE ON accounts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_projection();
CREATE CONSTRAINT TRIGGER entry_projection AFTER INSERT ON entries DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_projection();
CREATE TABLE intents (
  id uuid PRIMARY KEY, merchant_id text NOT NULL REFERENCES accounts(id),
  payer_id text REFERENCES accounts(id), amount bigint NOT NULL CHECK (amount > 0),
  refunded bigint NOT NULL DEFAULT 0 CHECK (refunded >= 0 AND refunded <= amount),
  status text NOT NULL CHECK (status IN ('CREATED','AUTHORIZED','CAPTURED','REFUNDED','VOIDED')),
  expires_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX expiring_intents ON intents(expires_at) WHERE status = 'AUTHORIZED';
CREATE TABLE idempotency (
  actor text NOT NULL, key text NOT NULL, fingerprint text NOT NULL,
  response text, created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(actor, key)
);
CREATE TABLE outbox (
  id uuid PRIMARY KEY, payload text NOT NULL, status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','SENT','DEAD')),
  attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz
);
CREATE INDEX outbox_pending ON outbox(next_attempt) WHERE status = 'PENDING';
CREATE TABLE inbox (event_id uuid PRIMARY KEY, processed_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE webhook_deliveries (
  event_id uuid PRIMARY KEY REFERENCES inbox(event_id), payload text NOT NULL,
  status text NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING','DELIVERED','DEAD')),
  attempts integer NOT NULL DEFAULT 0, next_attempt timestamptz NOT NULL DEFAULT now(),
  last_result text, delivered_at timestamptz
);
CREATE INDEX webhook_pending ON webhook_deliveries(next_attempt) WHERE status = 'PENDING';
