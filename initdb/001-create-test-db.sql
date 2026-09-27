-- Runs once, only when the postgres data volume is first initialized.
-- Gives the test suite its own database, separate from the dev database
-- (agent_relay) that this same postgres service also serves, so `pytest`
-- never drops/recreates tables the dev server is using.
CREATE DATABASE agent_relay_test;
