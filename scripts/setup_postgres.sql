-- Create a dedicated database and user for the parts app.
-- Run as a superuser:   psql -U postgres -f scripts/setup_postgres.sql
-- Change the password below before running (and use the same one in DATABASE_URL).

CREATE DATABASE partsid;
CREATE USER partsid_user WITH PASSWORD 'change-this-password';
GRANT ALL PRIVILEGES ON DATABASE partsid TO partsid_user;

-- PostgreSQL 15+ needs schema rights too; connect to the new database first.
\c partsid
GRANT ALL ON SCHEMA public TO partsid_user;
GRANT ALL ON SCHEMA public TO public;
