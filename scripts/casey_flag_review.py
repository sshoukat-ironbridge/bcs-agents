"""Writes out/flag_review.html (local only, gitignored): every Critical
and High flag with its evidence quote, the 10 highest-cost jobs by
number of analyzed messages, and PMs whose mailbox check failed. Never
prints a quote or any customer text to the terminal — only this script
writes them, into the local HTML file.

Usage:
    docker compose run --rm casey python scripts/casey_flag_review.py
"""
import html
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

OUT_PATH = Path(__file__).parent.parent / "out" / "flag_review.html"


def _critical_high_flags() -> list[dict]:
    from db.state_store import _Session, CaseyFlag, MessageAnalysis, CustomerMessage

    with _Session() as s:
        flags = (
            s.query(CaseyFlag)
            .filter(CaseyFlag.severity.in_(["Critical", "High"]), CaseyFlag.is_demo == False)  # noqa: E712
            .order_by(CaseyFlag.severity.desc(), CaseyFlag.created_at.asc())
            .all()
        )
        rows = []
        for f in flags:
            evidence_quote, source_desc = None, None
            if f.first_message_id:
                analysis = (
                    s.query(MessageAnalysis)
                    .filter(MessageAnalysis.message_id == f.first_message_id)
                    .order_by(MessageAnalysis.created_at.desc())
                    .first()
                )
                message = s.query(CustomerMessage).filter_by(id=f.first_message_id).first()
                if analysis:
                    evidence_quote = analysis.evidence_quote
                if message:
                    channel = "email" if message.source == "gmail" else "call" if message.source == "dialpad_call" else message.source
                    source_desc = f"{channel}, {message.direction}"
            rows.append({
                "id": f.id, "customer_name": f.customer_name, "address": f.address or f.job_key,
                "pm_name": f.pm_name, "severity": f.severity,
                "first_detected": f.created_at.strftime("%b %d, %Y") if f.created_at else "—",
                "issue_summary": f.issue_summary or "", "evidence_quote": evidence_quote or "(none)",
                "source": source_desc or "—",
            })
        return rows


def _top_cost_jobs(top: int = 10) -> list[dict]:
    from db.state_store import _Session, MessageAnalysis, CustomerMessage
    from collections import Counter

    with _Session() as s:
        rows = (
            s.query(CustomerMessage.job_key, CustomerMessage.customer_email, CustomerMessage.pm_name)
            .join(MessageAnalysis, MessageAnalysis.message_id == CustomerMessage.id)
            .filter(CustomerMessage.is_demo == False)  # noqa: E712
            .all()
        )
    counts = Counter(job_key for job_key, _, _ in rows if job_key)
    label_by_key = {job_key: (job_key, pm_name) for job_key, _email, pm_name in rows}
    ranked = counts.most_common(top)
    return [{"job_key": k, "pm_name": label_by_key.get(k, (k, ""))[1], "analyzed_count": c} for k, c in ranked]


def _failed_mailboxes() -> list[dict]:
    import scripts.casey_coverage as coverage
    results = coverage.check_mailboxes()
    return [{"pm_name": r["pm_name"], "email": email, "error": r["error"]}
            for email, r in results.items() if not r["ok"]]


def build_html() -> str:
    flags = _critical_high_flags()
    top_jobs = _top_cost_jobs()
    failed_mailboxes = _failed_mailboxes()

    def esc(s):
        return html.escape(str(s))

    flag_rows = "\n".join(
        f"""<tr>
          <td class="sev sev-{esc(f['severity'].lower())}">{esc(f['severity'])}</td>
          <td>{esc(f['customer_name'])}</td>
          <td>{esc(f['address'])}</td>
          <td>{esc(f['pm_name'])}</td>
          <td>{esc(f['first_detected'])}</td>
          <td>{esc(f['source'])}</td>
          <td>{esc(f['issue_summary'])}</td>
          <td class="quote">&ldquo;{esc(f['evidence_quote'])}&rdquo;</td>
        </tr>"""
        for f in flags
    )

    top_jobs_rows = "\n".join(
        f"<tr><td>{esc(j['job_key'])}</td><td>{esc(j['pm_name'])}</td><td>{esc(j['analyzed_count'])}</td></tr>"
        for j in top_jobs
    )

    failed_mailbox_rows = "\n".join(
        f"<tr><td>{esc(m['pm_name'])}</td><td>{esc(m['email'])}</td><td>{esc(m['error'])}</td></tr>"
        for m in failed_mailboxes
    ) or "<tr><td colspan='3'>None — every mailbox checked OK.</td></tr>"

    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>Casey Flag Review (local only)</title>
<style>
  body {{ font-family: -apple-system, Arial, sans-serif; margin: 2rem; color: #1e293b; }}
  h1 {{ font-size: 1.4rem; }}
  h2 {{ font-size: 1.1rem; margin-top: 2rem; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 0.5rem; }}
  th, td {{ border: 1px solid #e2e8f0; padding: 6px 10px; font-size: 0.85rem; text-align: left; vertical-align: top; }}
  th {{ background: #f8fafc; }}
  .quote {{ font-style: italic; color: #475569; max-width: 320px; }}
  .sev {{ font-weight: 700; }}
  .sev-critical {{ color: #dc2626; }}
  .sev-high {{ color: #ea580c; }}
  .note {{ color: #64748b; font-size: 0.85rem; }}
</style>
</head>
<body>
<h1>Casey Flag Review — local only, never committed</h1>
<p class="note">{len(flags)} Critical/High flag(s).</p>

<h2>Critical and High Flags</h2>
<table>
<tr><th>Severity</th><th>Customer</th><th>Project</th><th>PM</th><th>First Detected</th><th>Source</th><th>Issue Summary</th><th>Evidence Quote</th></tr>
{flag_rows}
</table>

<h2>Top 10 Highest-Cost Jobs (by analyzed message count)</h2>
<table>
<tr><th>Job</th><th>PM</th><th>Analyzed Messages</th></tr>
{top_jobs_rows}
</table>

<h2>Mailboxes That Failed the Check</h2>
<table>
<tr><th>PM</th><th>Email</th><th>Error</th></tr>
{failed_mailbox_rows}
</table>

</body>
</html>
"""


def main() -> None:
    OUT_PATH.parent.mkdir(exist_ok=True)
    content = build_html()
    OUT_PATH.write_text(content)
    print(f"Wrote {OUT_PATH} ({len(content)} bytes)")


if __name__ == "__main__":
    main()
