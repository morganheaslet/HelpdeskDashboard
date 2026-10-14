/*
  th2-metrics-db — fuller Dialpad per-agent-day stats for the Power BI
  Phones page, from the same stats export (group_by=user) already pulled.

  Durations are SECONDS. Dialpad's export reports minutes; until
  2026-10-14 they were stored as if they were seconds (60x too small), so
  after deploying the matching sync code, re-pull the history:
      UPDATE meta.sync_state SET cursor_utc = NULL, backfill_complete = 0,
             cursor_id = NULL, last_daily_job_date = NULL
      WHERE entity = 'dp_agent_daily';
  The next SyncMetrics runs re-backfill from 2025-01-01 in 30-day chunks,
  overwriting every row (MERGE) with corrected durations + these columns.

  "missed" is per agent: a call that rings a whole hunt group and is
  answered by a teammate still counts as missed for everyone else it rang.
  ring_no_answer / rejected are the agent-specific signals.
*/
IF COL_LENGTH('dp.agent_daily', 'outbound_calls') IS NULL
    ALTER TABLE dp.agent_daily ADD
        all_calls           INT NOT NULL CONSTRAINT DF_ad_all DEFAULT 0,
        outbound_calls      INT NOT NULL CONSTRAINT DF_ad_out DEFAULT 0,
        outbound_connected  INT NOT NULL CONSTRAINT DF_ad_outc DEFAULT 0,
        handled             INT NOT NULL CONSTRAINT DF_ad_hand DEFAULT 0,
        rejected            INT NOT NULL CONSTRAINT DF_ad_rej DEFAULT 0,
        ring_no_answer      INT NOT NULL CONSTRAINT DF_ad_rna DEFAULT 0,
        voicemails          INT NOT NULL CONSTRAINT DF_ad_vm DEFAULT 0,
        transferred_in      INT NOT NULL CONSTRAINT DF_ad_tin DEFAULT 0,
        transferred_out     INT NOT NULL CONSTRAINT DF_ad_tout DEFAULT 0,
        hold_seconds        DECIMAL(12,1) NOT NULL CONSTRAINT DF_ad_hold DEFAULT 0,
        dialpad_user_id     NVARCHAR(50)  NULL,
        email               NVARCHAR(200) NULL,
        agent_type          NVARCHAR(50)  NULL;
GO
