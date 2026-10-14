"""
Timer trigger (every 15 minutes, offset 7 minutes from PullSnapshot so the
two don't hit ConnectWise's rate limit at the same moment): syncs
ConnectWise, Dialpad and SmileBack into th2-metrics-db. Runs alongside the
existing blob-based PullSnapshot until the dashboard reads from SQL.
"""
import azure.functions as func

from shared.sync_all import run_sync


def main(timer: func.TimerRequest) -> None:
    run_sync()
