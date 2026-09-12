"""
HTTP-triggered function that serves the latest report data blob to the frontend.

authLevel is "anonymous" at the Function level because access control happens
one layer up: when this Function App is linked as a Static Web App's managed
API, staticwebapp.config.json's route rules require an authenticated (Entra ID)
session before a request ever reaches here — see static-web-app/staticwebapp.config.json.
If you ever call this function directly (not through the Static Web App), put
it behind Easy Auth or an API key.
"""
import json
import logging
import os

import azure.functions as func
from azure.storage.blob import BlobServiceClient


def main(req: func.HttpRequest) -> func.HttpResponse:
    try:
        conn_str = os.environ["STORAGE_CONNECTION_STRING"]
        container = os.environ.get("DATA_CONTAINER", "helpdesk-report-data")
        service = BlobServiceClient.from_connection_string(conn_str)
        blob = service.get_blob_client(container=container, blob="latest.json")
        data = blob.download_blob().readall()
        return func.HttpResponse(data, mimetype="application/json", status_code=200)
    except Exception as exc:
        logging.exception("GetReportData failed")
        return func.HttpResponse(
            json.dumps({"error": "Report data not available yet", "detail": str(exc)}),
            mimetype="application/json",
            status_code=503,
        )
