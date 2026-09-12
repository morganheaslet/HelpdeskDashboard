"""
Stub NinjaOne client.

Fill this in once you register an API application in NinjaOne (Administration >
Apps > API). NinjaOne uses OAuth2 client-credentials — NINJAONE_CLIENT_ID and
NINJAONE_CLIENT_SECRET (set as Function App settings, backed by Key Vault same
as the ConnectWise keys) exchange for a bearer token at
POST https://<region>.ninjarmm.com/ws/oauth/token.

This isn't in the current report, but is a natural addition for a helpdesk
manager's shift-check tab: device health alerts, offline device counts, patch
compliance — anything that might correlate with ticket volume. Left as a stub
so the aggregation pipeline has a slot for it when you're ready.
"""
import os


class NinjaOneClient:
    def __init__(self):
        self.client_id = os.environ.get("NINJAONE_CLIENT_ID", "")
        self.client_secret = os.environ.get("NINJAONE_CLIENT_SECRET", "")

    def get_open_alerts_summary(self):
        """
        Intended return shape:
        {
          "totalOpenAlerts": 0,
          "criticalDevicesOffline": 0,
          "byCompany": {"Company Name": {"alerts": 0, "offlineDevices": 0}}
        }
        """
        if not self.client_id or self.client_id == "not-yet-configured":
            raise NotImplementedError(
                "NinjaOne API credentials not configured — register an API app in NinjaOne, "
                "set NINJAONE_CLIENT_ID/NINJAONE_CLIENT_SECRET, then implement the OAuth2 "
                "token exchange and the /v2/alerts call here."
            )
        raise NotImplementedError("NinjaOne API call not yet implemented — see module docstring.")
