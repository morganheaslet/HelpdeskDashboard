/*
  th2-metrics-db — database access. Run as the server's Entra admin.

  The Function App (th2-helpdesk-functions) connects with its system-assigned
  managed identity. The user is created from the identity's application
  (client) id via WITH SID, which — unlike FROM EXTERNAL PROVIDER — doesn't
  require the SQL server itself to have Microsoft Graph read permissions.
  If the Function App's identity is ever recreated, its appId changes:
    az ad sp show --id <principalId> --query appId -o tsv
  then drop and re-run this.

  Read/write data only; schema changes are applied by an admin running the
  numbered scripts, never by the sync.
*/
IF NOT EXISTS (SELECT 1 FROM sys.database_principals WHERE name = 'th2-helpdesk-functions')
    CREATE USER [th2-helpdesk-functions]
        WITH SID = 0xDC5BB61821678C4097623A0C59B8F16F, TYPE = E;  -- appId 18b65bdc-6721-408c-9762-3a0c59b8f16f
GO
ALTER ROLE db_datareader ADD MEMBER [th2-helpdesk-functions];
ALTER ROLE db_datawriter ADD MEMBER [th2-helpdesk-functions];
GO

-- Read-only role for Power BI / analysts: the reporting views plus the raw
-- tables (so report authors can drill down). Add an Entra group to it, e.g.
--   CREATE USER [Helpdesk Reporting] FROM EXTERNAL PROVIDER;
--   ALTER ROLE reporting_reader ADD MEMBER [Helpdesk Reporting];
IF DATABASE_PRINCIPAL_ID('reporting_reader') IS NULL
    CREATE ROLE reporting_reader;
GO
GRANT SELECT ON SCHEMA::rpt  TO reporting_reader;
GRANT SELECT ON SCHEMA::cw   TO reporting_reader;
GRANT SELECT ON SCHEMA::dp   TO reporting_reader;
GRANT SELECT ON SCHEMA::sb   TO reporting_reader;
GRANT SELECT ON SCHEMA::meta TO reporting_reader;
GO
