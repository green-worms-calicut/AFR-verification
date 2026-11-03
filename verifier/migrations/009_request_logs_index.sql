-- database.other_logs finds the requests holding a collection log id with `body @> ...`.
CREATE INDEX verification_request_body ON verification_request USING gin (body jsonb_path_ops);
-- ...or a trip within a day (the other way database.other_logs finds requests), so the two can be combined.
CREATE INDEX verification_request_trip_day ON verification_request (trip_day);
