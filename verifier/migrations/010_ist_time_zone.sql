-- Show the database's times in IST (+05:30), like the photos' GPS stamps, in every new session. Times stay stored as
-- instants and no query depends on the session's zone (database.day_report names Asia/Kolkata itself). Takes effect
-- for connections opened after it; the app's login owns the database, so it may set this.
DO $$
BEGIN
    EXECUTE format('ALTER DATABASE %I SET timezone TO %L', current_database(), 'Asia/Kolkata');
END
$$;
