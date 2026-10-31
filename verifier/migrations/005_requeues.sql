-- How many times the sweep queued a request's check, or a result's send, again after its job crashed or went
-- missing, so a crash that repeats stops at tasks.MAX_REQUEUES instead of looping.
ALTER TABLE verification_request ADD COLUMN requeues integer NOT NULL DEFAULT 0;
ALTER TABLE verification_result ADD COLUMN requeues integer NOT NULL DEFAULT 0;
