-- One Postgres server, one database per service (each service owns its data).
-- In AWS we run a single small RDS instance for cost reasons; production would often give
-- each service its own instance or at least its own credentials.
CREATE DATABASE deploydb;
CREATE DATABASE incidentdb;
