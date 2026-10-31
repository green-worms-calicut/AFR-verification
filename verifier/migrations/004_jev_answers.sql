-- Jev's answers for each image of a request, saved as soon as each image is answered, so a retry of the same request
-- (Jev down, a crash) asks only the images not answered yet. Keyed by proof_check's answer_key.
CREATE TABLE jev_answer (
    request_id uuid NOT NULL REFERENCES verification_request,
    answer_key text NOT NULL,
    raw        jsonb NOT NULL,
    made_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (request_id, answer_key)
);
