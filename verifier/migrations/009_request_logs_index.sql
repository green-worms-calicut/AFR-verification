-- database.other_logs finds the requests holding a collection log id with `body @> ...`.
CREATE INDEX verification_request_body ON verification_request USING gin (body jsonb_path_ops);
