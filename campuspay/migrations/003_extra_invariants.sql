ALTER TABLE journals ADD COLUMN created_xid xid8 NOT NULL DEFAULT pg_current_xact_id();
CREATE FUNCTION prevent_late_entries() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF (SELECT created_xid FROM journals WHERE id = NEW.journal_id) <> pg_current_xact_id()
    THEN RAISE EXCEPTION 'cannot append to committed journal'; END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER no_late_entries BEFORE INSERT ON entries FOR EACH ROW EXECUTE FUNCTION prevent_late_entries();
ALTER TABLE intents ADD CONSTRAINT intent_payer CHECK (
  (status='CREATED' AND payer_id IS NULL) OR status='VOIDED' OR
  (status IN ('AUTHORIZED','CAPTURED','REFUNDED') AND payer_id IS NOT NULL AND expires_at IS NOT NULL)
);
CREATE FUNCTION check_reserved() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE aid text; held numeric; actual bigint;
BEGIN
  IF TG_TABLE_NAME = 'accounts' THEN aid := NEW.id; ELSE aid := NEW.payer_id; END IF;
  IF aid IS NULL THEN RETURN NULL; END IF;
  SELECT reserved INTO actual FROM accounts WHERE id=aid;
  SELECT COALESCE(SUM(amount::numeric),0) INTO held FROM intents WHERE payer_id=aid AND status='AUTHORIZED';
  IF held <> actual THEN RAISE EXCEPTION 'hold projection mismatch %', aid; END IF;
  RETURN NULL;
END $$;
CREATE CONSTRAINT TRIGGER account_holds AFTER UPDATE ON accounts DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_reserved();
CREATE CONSTRAINT TRIGGER intent_holds AFTER INSERT OR UPDATE ON intents DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION check_reserved();
CREATE INDEX intents_payer ON intents(payer_id) WHERE status='AUTHORIZED';
