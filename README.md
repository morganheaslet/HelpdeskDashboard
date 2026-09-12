# Helpdesk Ops Report — Azure Hosting Scaffold

This turns the manually-refreshed report into a scheduled, always-current one that your
team can open in a browser, backed by Azure.

## Architecture

```
                    ┌─────────────────────────────┐
   Timer trigger →  │  Azure Function App          │
   (every 15 min)   │  - PullSnapshot               │──► Blob Storage
                    │    (ConnectWise + Dialpad;    │    /data/latest.json
                    │     NinjaOne still a stub)     │    /data/history/*.json
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

1. **Function App** (`function-app/`) — a timer-triggered function (`PullSnapshot`)
   polls ConnectWise and Dialpad on a schedule and writes a single `latest.json`
   blob containing everything the report needs — the same shape as the `data.json`
   currently embedded in the artifact. A second, HTTP-triggered function
   (`GetReportData`) serves that blob to the frontend. A third, HTTP-triggered
   function (`RefreshNow`) does the exact same pull as `PullSnapshot`, on demand —
   it's what the report's "Refresh Now" button calls, for whenever fifteen minutes
   is too long to wait. All three share the same pull-and-merge logic
   (`shared/refresh.py`) so they can't drift out of sync with each other.
   Credentials never touch the browser — they live in the Function App's settings,
   backed by Key Vault.

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
- **Dialpad**: `function-app/shared/dialpad_client.py` is now a real client against
  Dialpad's Stats Export API — `PullSnapshot` calls it every run and, once
  `DIALPAD_API_KEY` is set, writes live `answered`/`missed`/`avgTalkTimeSeconds`/
  per-agent numbers into the blob as `todayPhone`. The frontend's Today's Snapshot
  tab shows a live "Phones — live (Dialpad)" card when that key is present in the
  data, and falls back to its old "not live yet" note otherwise — so this is safe
  to deploy before you've generated a Dialpad API key; it just won't do anything
  until you have. One caveat: Dialpad doesn't publish an exact CSV column list for
  the stats export, so the client matches columns by name pattern (see the big
  comment at the top of `dialpad_client.py`) — **the first time this runs for
  real, check the Function App's logs and sanity-check the numbers against
  Dialpad's own analytics dashboard once**, and adjust the column-matching
  patterns there if anything looks off. This only replaces the *live, today*
  phone numbers — the "Phones & Historical" tab's weekly trend is still the
  manually-reported (BrightGauge) data, unchanged.
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
# NOTE: --os-type linux is required — Azure Functions' Python runtime only runs
# on Linux, and `az functionapp create` defaults to Windows if you omit this,
# which fails with "Runtime python not supported for os windows".
az functionapp create -g rg-helpdesk-report -n th2-helpdesk-functions \
  --consumption-plan-location eastus --os-type linux \
  --runtime python --runtime-version 3.11 \
  --functions-version 4 --storage-account th2helpdeskdata

# 4. Key Vault for the ConnectWise API keys
# NOTE: `az keyvault create` defaults to RBAC authorization mode now (not the
# older "access policy" model), and a vault created that way rejects
# `az keyvault set-policy` with "Cannot set policies to a vault with
# '--enable-rbac-authorization' specified". Step 5 below uses an RBAC role
# assignment instead, which is the correct match for that default.
az keyvault create -n th2-helpdesk-kv -g rg-helpdesk-report -l eastus
az keyvault secret set --vault-name th2-helpdesk-kv -n CwPrivateKey --value "<connectwise private key>"

# 5. Grant the Function App's managed identity access to the vault (RBAC role
# assignment — use this instead of `az keyvault set-policy` unless you created
# the vault with `--enable-rbac-authorization false`)
az functionapp identity assign -g rg-helpdesk-report -n th2-helpdesk-functions
# grab the vault's resource id and the identity's principalId, then:
az role assignment create \
  --role "Key Vault Secrets User" \
  --assignee <identity principalId from previous command> \
  --scope $(az keyvault show -n th2-helpdesk-kv -g rg-helpdesk-report --query id -o tsv)

# 6. App settings (non-secret config; the secret is a Key Vault reference)
# NOTE: SCM_DO_BUILD_DURING_DEPLOYMENT=true is required — without it, whatever
# environment ran `pip install` (e.g. a GitHub Actions runner) ships its own
# prebuilt wheels straight into the Function App, and packages with compiled
# extensions (cryptography, used by azure-storage-blob) fail at runtime with
# "ImportError: ... GLIBC_2.33' not found" because that build environment's
# glibc doesn't match the Function App container's. This setting tells Azure
# to install dependencies itself, remotely, in a container that matches.
az functionapp config appsettings set -g rg-helpdesk-report -n th2-helpdesk-functions --settings \
  SCM_DO_BUILD_DURING_DEPLOYMENT="true" \
  CW_COMPANY_ID="<connectwise company id>" \
  CW_PUBLIC_KEY="<connectwise public api key>" \
  CW_PRIVATE_KEY="@Microsoft.KeyVault(SecretUri=https://th2-helpdesk-kv.vault.azure.net/secrets/CwPrivateKey/)" \
  CW_CLIENT_ID="<connectwise clientId issued for your API member>" \
  CW_SITE="na.myconnectwise.net" \
  STORAGE_CONNECTION_STRING="<from the storage account's access keys>" \
  DIALPAD_API_KEY="<generate at Dialpad Admin Settings > Integrations > API>" \
  DIALPAD_OFFICE_ID="<optional — from GET /api/v2/offices if calls need a target scope>"

# 7. Deploy the function code
cd function-app
func azure functionapp publish th2-helpdesk-functions

# 8. Static Web App — create it WITHOUT a --source, then push the files
# straight from your machine with the SWA CLI. (The alternative — --source
# plus --login-with-github — only works if this scaffold is already pushed to
# a real GitHub repo, since it has Azure generate a GitHub Actions workflow IN
# that repo and walks you through a GitHub device login. If you're just
# working from the unzipped folder locally, as most people are at this step,
# skip that entirely and use the CLI push below instead.)
az staticwebapp create -n th2-helpdesk-report -g rg-helpdesk-report -l eastus2 --sku Free

# One-time: install the Static Web Apps CLI (needs Node.js)
npm install -g @azure/static-web-apps-cli

# Grab this Static Web App's deployment token, then push the static-web-app
# folder's contents to it directly:
cd static-web-app
swa deploy . --env production --deployment-token $(az staticwebapp secrets list -n th2-helpdesk-report --query "properties.apiKey" -o tsv)
cd ..

# Managed API linking (SWA + separate Function App, "bring your own functions"):
az staticwebapp backends link -n th2-helpdesk-report \
  --backend-resource-id $(az functionapp show -g rg-helpdesk-report -n th2-helpdesk-functions --query id -o tsv) \
  --backend-region eastus
```

Steps 1–6 are one-time. Step 7 (`func azure functionapp publish`) is how you push
code updates to the data-pulling side — rerun it any time `function-app/` changes.
Step 8's `swa deploy` is the equivalent for the frontend — rerun it any time
`static-web-app/index.html` changes (e.g. after Claude republishes the report).

### Using GitHub Actions instead of `swa deploy` (recommended if the SWA CLI gives you trouble)

This scaffold already ships a working workflow file at
`.github/workflows/azure-static-web-apps.yml` that deploys *both* the Static Web
App and the Function App on every push to `main`. If the local `swa deploy`
step above fails for you (e.g. a broken deployment-binary download), this path
sidesteps it entirely — GitHub's own runners do the deploy, not a binary on
your machine. It doesn't require recreating anything you already made above.

1. Create a new repo on github.com (Settings gear → your profile → **New repository**;
   private is fine), then from the unzipped scaffold folder:
   ```
   git init
   git add .
   git commit -m "Initial commit"
   git branch -M main
   git remote add origin https://github.com/<your-username>/<repo-name>.git
   git push -u origin main
   ```
2. Add two repo secrets — on GitHub, go to the repo → **Settings → Secrets and
   variables → Actions → New repository secret**:
   - `AZURE_STATIC_WEB_APPS_API_TOKEN` — value from:
     ```
     az staticwebapp secrets list -n th2-helpdesk-report --query "properties.apiKey" -o tsv
     ```
   - `AZURE_FUNCTIONAPP_PUBLISH_PROFILE` — value from:
     ```
     az functionapp deployment list-publishing-profiles -g rg-helpdesk-report -n th2-helpdesk-functions --xml
     ```
     (paste the entire XML output as the secret's value)
3. That's it — the push in step 1 already triggered the workflow once (check
   the repo's **Actions** tab for its run), and every future `git push` to
   `main` redeploys both the frontend and the function code automatically.

If you'd rather have Azure generate its own separate GitHub Actions workflow
instead of using the one already in this scaffold, that's what the
`--source`/`--login-with-github` option on `az staticwebapp create` is for —
but don't use both approaches on the same repo; pick one. That would mean
re-creating the Static Web App with `--source https://github.com/<your-org>/<repo>`
pointed at your new repo, which Azure then wires up itself. It's an optional
alternative, not something you need once the steps above are working.

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
