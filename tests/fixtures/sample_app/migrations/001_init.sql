-- Initial schema. The migration that OWNS the `accounts` table (ALTER) — code that queries it is
-- structurally downstream even though there is NO code edge between them (the schema-graph coupling).
CREATE TABLE accounts (
    id   int PRIMARY KEY,
    name text NOT NULL
);

ALTER TABLE accounts ADD COLUMN active boolean DEFAULT true;
