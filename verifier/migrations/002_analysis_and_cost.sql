-- check_proof's whole result for a request (every image's answers, readings and fingerprints, every check),
-- kept for people looking into a flag and for comparing disposals later; and what asking Jev cost.
ALTER TABLE verification_result ADD COLUMN analysis jsonb;
ALTER TABLE verification_result ADD COLUMN cost_usd numeric(10, 6);
