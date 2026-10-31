-- When the worker last ran its every-minute sweep, so the web process can tell when the worker has gone silent.
CREATE TABLE worker_heartbeat (
    id      integer PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    beat_at timestamptz NOT NULL
);
