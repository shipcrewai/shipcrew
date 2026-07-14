-- Dolt SQL server initialization script.
-- Creates a network-accessible user for Beads (bd) in addition to the
-- localhost-only user created by the Dolt entrypoint from DOLT_USER.

CREATE USER IF NOT EXISTS 'shipply'@'%' IDENTIFIED BY 'shipply';
GRANT ALL PRIVILEGES ON shipply_beads.* TO 'shipply'@'%';
