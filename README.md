# Helpdesk Ops Report — Azure Hosting Scaffold

This turns the manually-refreshed report into a scheduled, always-current one that your
team can open in a browser, backed by Azure.

## Architecture

```
                    ┌─────────────────────────────┐
   Timer trigger →  │  Azure Function App          │
   (every 15 min)   │  - PullSnapshot               │──► Blob Storage
                    │    (ConnectWise + stubs for   │    /data/latest.json
                    │     Dialpad, NinjaOne)         │    /data/history/*.json
                    │  - GetReportData (HTTP)        │◄── reads latest.json
                    └─────────────────────────────┘
                                   ▲
                                   │ /api/GetReportData
                    ┌─────────────────────────────┐
                    │  Azure Static Web App         │
                    │  report.html + assets         │──► served to team,
                    │  gated by Entra ID (M365)     │    auth built in
                    └─────────────────────────────┘
```

Two pieces, both serverless (pay only when they run/serve traffic):

1. **Function App** (`function-app/`) — a timer-triggered function polls ConnectWise
   (and, once you're ready, Dialpad and NinjaOne) on a schedule and writes a single
   `latest.json` blob containing everything the report needs — the same shape as the
   `data.json` currently embedded in the artifact. A second, HTTP-triggered function
   serves that blob to the frontend. Credentials never touch the browser — they live
   in the Function App's settings, backed by Key Vault.

2. **Static Web App** (`static-web-app/`) — the report UI (same HTML/CSS/JS you've
   already seen), modified to fetch its data from `/api/GetReportData` instead of
   having it baked in at publish time. Azure Static Web Apps links directly to the
   Function App as its "managed API," so this is one deployment, one URL, and
   Azure handles the API auth token exchange automatically.

Since TH2 is on Microsoft 365, Static Web Apps' built-in Entra ID (AAD) auth is the
lowest-effort way to restrict this to your team — no separate login system, no
password to manage, just "sign in with your work account." That's wired up in
`static-web-app/staticwebapp.config.json`.

## What's real vs. stubbed right now

- **ConnectWise**: `function-app/shared/cw_client.py` is a real REST client against
  the ConnectWise Manage API (ticket lists, statuses, members) — it's the same logic
  used to build the current report, ported from the interactive MCP calls to direct
  HTTP calls (which is what has to happen outside this chat session anyway).
- **Dialpad**: `function-app/shared/dialpad_client.py` is a stub with the shape of
  what it will return (calls answered/missed, talk time, by-agent breakdown) and a
  `NotImplementedError` where the real API call goes, plus a comment on what
  Dialpad API scope/token you'll need. Phone metrics stay manual (BrightGauge) in
  the report until this is filled in.
- **NinjaOne**: `function-app/shared/ninjaone_client.py` is the same kind of stub,
  for device health / alert counts if you want that folded into the report later.

Filling in a stub is a matter of dropping in the real API call inside the existing
function signature — the aggregation and report-rendering code doesn't need to
change.

## Deploying it

You'll need the Azure CLI (`az`) logged into TH2's subscription, and Owner/Contributor
rights to create resources. Rough one-time setup:

```bash
# 1. Resource group
az group create -n rg-helpdesk-report -l eastus

# 2. Storage account (holds the ConnectWise credentials-free data blob)
az storage account create -n th2helpdeskdata -g rg-helpdesk-report -l eastus --sku Standard_LRS

# 3. Function App (Python 3.11, consumption plan — scales to zero, cheap)
az functionapp create -g rg-helpdesk-report -n th2-helpdesk-functions \
  --consumption-plan-location eastus --runtime python --runtime-version 3.11 \
  --functions-version 4 --storage-account th2helpdeskdata

# 4. Key Vault for the ConnectWise API keys
az keyvault create -n th2-helpdesk-kv -g rg-helpdesk-report -l eastus
az keyvault secret set --vault-name th2-helpdesk-kv -n CwPrivateKey --value "<connectwise private key>"

# 5. Grant the Function App's managed identity access to the vault
az functionapp identity assign -g rg-helpdesk-report -n th2-helpdesk-functions
az keyvault set-policy -n th2-helpdesk-kv \
  --object-id <identity principalId from previous command> --secret-permissions get

# 6. App settings (non-secret config; the secret is a Key Vault reference)
az functionapp config appsettings set -g rg-helpdesk-report -n th2-helpdesk-functions --settings \
  CW_COMPANY_ID="<connectwise company id>" \
  CW_PUBLIC_KEY="<connectwise public api key>" \
  CW_PRIVATE_KEY="@Microsoft.KeyVault(SecretUri=https://th2-helpdesk-kv.vault.azure.net/secrets/CwPrivateKey/)" \
  CW_CLIENT_ID="<connectwise clientId issued for your API member>" \
  CW_SITE="na.myconnectwise.net" \
  STORAGE_CONNECTION_STRING="<from the storage account's access keys>"

# 7. Deploy the function code
cd function-app
func azure functionapp publish th2-helpdesk-functions

# 8. Static Web App, linked to the Function App as its managed API
az staticwebapp create -n th2-helpdesk-report -g rg-helpdesk-report -l eastus2 \
  --source https://github.com/<your-org>/<this-repo> --branch main \
  --app-location "/static-web-app" --api-location "" \
  --login-with-github

# Managed API linking (SWA + separate Function App, "bring your own functions"):
az staticwebapp backends link -n th2-helpdesk-report \
  --backend-resource-id $(az functionapp show -g rg-helpdesk-report -n th2-helpdesk-functions --query id -o tsv) \
  --backend-region eastus
```

Steps 1–6 are one-time. Step 7 is how you push code updates to the data-pulling
side; Static Web Apps redeploys automatically on push to `main` once the GitHub
Actions workflow in `.github/workflows/azure-static-web-apps.yml` is wired up by
step 8 (it generates the workflow file and a deployment token secret for you).

### Turning on team sign-in

`static-web-app/staticwebapp.config.json` already routes everything through Entra ID
and restricts it to authenticated users. Two things to set after the SWA exists:

1. In the Azure Portal, open the Static Web App → **Settings → Authentication** and
   confirm the built-in Entra ID provider is enabled (it is, by default, for any
   Microsoft 365 tenant — no app registration needed for the basic case).
2. If you want to restrict it further than "anyone in the TH2 tenant" (e.g. just the
   helpdesk team), create a small Entra ID security group, add the team, and update
   the `allowedRoles` section of the config to check group membership via a custom
   role — happy to wire that in once the group exists.

## Schedule

`function-app/PullSnapshot/function.json` runs every 15 minutes
(`0 */15 * * * *`), which keeps "Today's Snapshot" close to live without hammering
the ConnectWise API. The historical/weekly-trend data doesn't need to run that
often — bump it to hourly by editing that cron expression if ConnectWise rate
limits become a concern.

## Local development

```bash
cd function-app
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp local.settings.json.example local.settings.json   # fill in your CW keys
func start
```

Then in `static-web-app/`, open `report.html` directly — it falls back to a bundled
sample `data.json` if `/api/GetReportData` isn't reachable, so you can iterate on
the UI without the function running.

## What this doesn't do yet

- Dialpad and NinjaOne pulls are stubbed (see above) — phone metrics and device
  health stay manually-entered (BrightGauge) until those are filled in.
- No alerting/notifications are wired up (e.g. Teams message if the pull fails) —
  worth adding once this is running for a few weeks and you know what "the pull
  failed" looks like in practice.
- No automated tests. For a scheduled data pull like this, the highest-value test
  is probably a smoke check that `latest.json` was updated in the last N minutes,
  which is cheap to add as an Azure Monitor alert on the blob's last-modified time.
