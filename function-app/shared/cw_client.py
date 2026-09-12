"""
Minimal ConnectWise Manage REST API client.

This replaces the interactive MCP `connectwise__*` tools used during development
with direct HTTP calls, since a scheduled Azure Function has no MCP server behind
it — it has to talk to ConnectWise's own REST API.

Auth: ConnectWise Manage uses HTTP Basic auth where the "username" is
`{companyId}+{publicKey}` and the "password" is the `privateKey`, plus a
`clientId` header (a GUID issued to your registered API application in
"My Extensions" / Developer Portal). All four values come from environment
variables set on the Function App (see local.settings.json.example / README.md).

Docs: https://developer.connectwise.com/Products/Manage/REST
"""
import os
import base64
import time
import requests

API_VERSION = "2022.1"  # ConnectWise Manage REST API version header


class ConnectWiseClient:
    def __init__(self):
        self.site = os.environ["CW_SITE"]
        self.company_id = os.environ["CW_COMPANY_ID"]
        self.public_key = os.environ["CW_PUBLIC_KEY"]
        self.private_key = os.environ["CW_PRIVATE_KEY"]
        self.client_id = os.environ["CW_CLIENT_ID"]
        self.base_url = f"https://{self.site}/v4_6_release/apis/3.0"

        auth_str = f"{self.company_id}+{self.public_key}:{self.private_key}"
        auth_b64 = base64.b64encode(auth_str.encode()).decode()
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Basic {auth_b64}",
            "clientId": self.client_id,
            "Accept": f"application/vnd.connectwise.com+json; version={API_VERSION}",
            "Content-Type": "application/json",
        })

    def _get_paged(self, path, params=None, page_size=1000, max_pages=20):
        """Fetch all pages of a list endpoint. ConnectWise caps pageSize at 1000."""
        params = dict(params or {})
        params["pageSize"] = page_size
        results = []
        page = 1
        while page <= max_pages:
            params["page"] = page
            resp = self.session.get(f"{self.base_url}{path}", params=params, timeout=30)
            resp.raise_for_status()
            batch = resp.json()
            results.extend(batch)
            if len(batch) < page_size:
                break
            page += 1
            time.sleep(0.2)  # be polite to the rate limiter
        return results

    def list_tickets(self, board_id, conditions=None, fields=None, closed_flag=None):
        """
        board_id: ConnectWise board id (24=Managed Services, 38=Technical Services,
                  20=Alerts, 54=Security Services on this instance).
        conditions: raw ConnectWise API condition string, e.g.
                    'closedDate>=[2026-09-12T00:00:00Z]'
        fields: list of field names to return (keeps payloads small); nested fields
                like lastUpdated must be requested as 'info/lastUpdated'.
        closed_flag: True/False to filter open vs. closed tickets, or None for both.
        """
        clauses = [f"board/id={board_id}"]
        if closed_flag is not None:
            clauses.append(f"closedFlag={'true' if closed_flag else 'false'}")
        if conditions:
            clauses.append(conditions)
        params = {"conditions": " AND ".join(clauses)}
        if fields:
            params["fields"] = ",".join(fields)
        return self._get_paged("/service/tickets", params)

    def list_statuses(self, board_id):
        resp = self.session.get(f"{self.base_url}/service/boards/{board_id}/statuses", timeout=30)
        resp.raise_for_status()
        return resp.json()

    def list_members(self):
        return self._get_paged("/system/members", {"fields": "identifier,firstName,lastName"})
