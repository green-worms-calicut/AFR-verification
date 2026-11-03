-- The fingerprints (md5 and dhash) of every image of each request, so an exact copy is found in any disposal, not only
-- the same vehicle's. Saved when a request is checked; the backfill below fills it from results already saved.
CREATE TABLE image_print (
    request_id uuid NOT NULL REFERENCES verification_request,
    kind       text NOT NULL,
    name       text NOT NULL,
    md5        text NOT NULL,
    dhash      text NOT NULL,
    PRIMARY KEY (request_id, kind, name)
);
CREATE INDEX image_print_md5 ON image_print (md5);
-- backfill
INSERT INTO image_print (request_id, kind, name, md5, dhash)
SELECT r.request_id, k.kind, img->>'name', img->'prints'->>'md5', img->'prints'->>'dhash'
FROM verification_result r
CROSS JOIN LATERAL jsonb_each(CASE WHEN jsonb_typeof(r.analysis->'images') = 'object'
                                   THEN r.analysis->'images' ELSE '{}'::jsonb END) AS k(kind, list)
CROSS JOIN LATERAL jsonb_array_elements(CASE WHEN jsonb_typeof(k.list) = 'array' THEN k.list ELSE '[]'::jsonb END) AS img
WHERE r.analysis IS NOT NULL AND img->>'name' IS NOT NULL
  AND img->'prints'->>'md5' IS NOT NULL AND img->'prints'->>'dhash' IS NOT NULL
ON CONFLICT DO NOTHING;
