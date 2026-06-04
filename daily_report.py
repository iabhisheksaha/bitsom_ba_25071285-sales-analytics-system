"""
Daily report generator — reads logs/applications.json and prints / saves
a formatted summary. Run standalone any time, or at end of orchestrator run.
"""

import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path


LOG_PATH = Path("logs/applications.json")
REPORTS_DIR = Path("logs/reports")


def load_log() -> dict:
    if not LOG_PATH.exists():
        return {}
    try:
        return json.loads(LOG_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def entries_for_date(log: dict, target: date) -> list[dict]:
    prefix = target.isoformat()
    return [e for e in log.values() if e.get("timestamp", "").startswith(prefix)]


def format_report(target: date, log: dict) -> str:
    entries = entries_for_date(log, target)
    submitted = [e for e in entries if e["status"] == "submitted"]
    failed    = [e for e in entries if e["status"] == "failed"]
    skipped   = [e for e in entries if e["status"] in ("skipped", "simulated")]

    lines = []
    lines.append("=" * 62)
    lines.append(f"  JOB APPLICATION DAILY REPORT — {target.strftime('%d %b %Y, %A')}")
    lines.append("=" * 62)

    # ── Today's submitted ──────────────────────────────────────────
    lines.append(f"\n✅  SUBMITTED ({len(submitted)})")
    if submitted:
        for e in submitted:
            ts = datetime.fromisoformat(e["timestamp"]).strftime("%H:%M")
            lines.append(f"    {ts}  [{e['platform'].upper():8s}]  {e['company'][:28]:28s}  {e['title'][:32]}")
    else:
        lines.append("    None")

    # ── Failed ────────────────────────────────────────────────────
    if failed:
        lines.append(f"\n❌  FAILED ({len(failed)})")
        for e in failed:
            reason = f"  ({e.get('reason','')})" if e.get("reason") else ""
            lines.append(f"    [{e['platform'].upper():8s}]  {e['company'][:28]:28s}  {e['title'][:32]}{reason}")

    # ── Skipped ───────────────────────────────────────────────────
    if skipped:
        lines.append(f"\n⚠️   SKIPPED / SIMULATED ({len(skipped)})")
        for e in skipped:
            lines.append(f"    [{e['platform'].upper():8s}]  {e['company'][:28]:28s}  {e.get('reason','')}")

    # ── Platform breakdown ────────────────────────────────────────
    if submitted:
        lines.append("\n─── Platform Breakdown ───────────────────────────────────")
        by_platform: dict[str, int] = defaultdict(int)
        for e in submitted:
            by_platform[e["platform"]] += 1
        for platform, count in sorted(by_platform.items()):
            bar = "█" * count
            lines.append(f"    {platform.title():10s}  {bar}  {count}")

    # ── All-time summary ──────────────────────────────────────────
    all_submitted = [e for e in log.values() if e["status"] == "submitted"]
    all_failed    = [e for e in log.values() if e["status"] == "failed"]
    lines.append("\n─── All-Time Totals ──────────────────────────────────────")
    lines.append(f"    Total submitted : {len(all_submitted)}")
    lines.append(f"    Total failed    : {len(all_failed)}")

    # Unique companies
    companies = {e["company"] for e in all_submitted}
    lines.append(f"    Unique companies: {len(companies)}")

    # Last 7 days trend
    lines.append("\n─── Last 7 Days ──────────────────────────────────────────")
    for i in range(6, -1, -1):
        day = target - timedelta(days=i)
        count = len([e for e in all_submitted
                     if e.get("timestamp", "").startswith(day.isoformat())])
        bar = "█" * count if count else "·"
        label = "today" if i == 0 else day.strftime("%a %d %b")
        lines.append(f"    {label:12s}  {bar}  {count if count else ''}")

    lines.append("\n" + "=" * 62)
    return "\n".join(lines)


def save_report(report_text: str, target: date) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    path = REPORTS_DIR / f"report_{target.isoformat()}.txt"
    path.write_text(report_text, encoding="utf-8")
    return path


def main():
    target = date.today()
    # Allow `python daily_report.py 2026-06-03` for historical reports
    if len(sys.argv) > 1:
        try:
            target = date.fromisoformat(sys.argv[1])
        except ValueError:
            print(f"Invalid date: {sys.argv[1]}. Use YYYY-MM-DD format.")
            sys.exit(1)

    log = load_log()
    report = format_report(target, log)
    print(report)

    saved = save_report(report, target)
    print(f"\n  Report saved → {saved}")


if __name__ == "__main__":
    main()
