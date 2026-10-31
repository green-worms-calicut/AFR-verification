-- Every request gw_sales sent, by requestId, and the outbox of results to post back.
-- verify_job and send_job are the Procrastinate jobs last queued for them, so the sweep can tell a lost one.
CREATE TABLE verification_request (
    request_id   uuid PRIMARY KEY,
    disposal_id  bigint NOT NULL,
    data_version text NOT NULL,
    reason       text NOT NULL,
    callback_url text NOT NULL,
    body         jsonb NOT NULL,
    received_at  timestamptz NOT NULL DEFAULT now(),
    verify_job   bigint
);
CREATE INDEX verification_request_disposal ON verification_request (disposal_id);

CREATE TABLE verification_result (
    request_id  uuid PRIMARY KEY REFERENCES verification_request,
    body        jsonb NOT NULL,
    made_at     timestamptz NOT NULL DEFAULT now(),
    send_job    bigint,
    attempts    integer NOT NULL DEFAULT 0,
    last_status integer,
    sent_at     timestamptz,
    gave_up_at  timestamptz,
    gave_up_why text
);
CREATE INDEX verification_result_unsent ON verification_result (made_at) WHERE sent_at IS NULL AND gave_up_at IS NULL;
