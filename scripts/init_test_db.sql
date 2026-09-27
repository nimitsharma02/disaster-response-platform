-- Runs once on first container start (docker-entrypoint-initdb.d).
-- Creates the dedicated test database so the suite never touches dev data.
CREATE DATABASE disaster_response_test;
