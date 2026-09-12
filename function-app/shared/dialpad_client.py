"""
Stub Dialpad client.

Fill this in once you generate a Dialpad API key (Admin Settings > Integrations >
API). Dialpad's REST API exposes call stats per office/department, which is what
would replace the manually-reported "Phones" section of the report
(calls answered, missed, average talk time, by-agent breakdown).

Suggested endpoint to start with: GET /api/v2/stats — accepts a date range and
target (office/department/user) and returns call volume aggregates. See
https://developers.dialpad.com/reference for the current shape.

Until this is filled in, PullSnapshot.py calls get_daily_call_stats() and, on the
NotImplementedError below, simply omits live phone data from the blob — the
frontend then shows the existing manually-reported (BrightGauge) numbers on the
"Phones & Historical" tab, unchanged.
"""
import os


class DialpadClient:
    def __init__(self):
        self.api_key = os.environ.get("DIALPAD_API_KEY", "")

    def get_daily_call_stats(self, date):
        """
        Intended return shape (to match what the report UI already expects):
        {
          "date": "2026-09-12",
          "answered": 0,
          "missed": 0,
          "avgTalkTimeSeconds": 0,
          "byAgent": {"Agent Name": {"answered": 0, "missed": 0}}
        }
        """
        if not self.api_key or self.api_key == "not-yet-configured":
            raise NotImplementedError(
                "Dialpad API key not configured — set DIALPAD_API_KEY in Function App settings "
                "once you've generated one, then implement the /api/v2/stats call here."
            )
        raise NotImplementedError("Dialpad API call not yet implemented — see module docstring.")
