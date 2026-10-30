-- The sweep looks only at the last week's requests (database.waiting), by when they were received.
CREATE INDEX verification_request_received ON verification_request (received_at);
