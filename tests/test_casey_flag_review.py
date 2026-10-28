import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from casey_flag_review import build_html  # noqa: E402


def test_build_html_includes_critical_high_flags_and_quote():
    flag_rows = [
        {"id": 1, "customer_name": "Jane Doe", "address": "123 Main St", "pm_name": "Gustavo",
         "severity": "Critical", "first_detected": "Oct 01, 2026",
         "issue_summary": "Customer threatened to cancel.", "evidence_quote": "this is a failed partnership",
         "source": "email, inbound"},
    ]
    with patch("casey_flag_review._critical_high_flags", return_value=flag_rows), \
         patch("casey_flag_review._top_cost_jobs", return_value=[]), \
         patch("casey_flag_review._failed_mailboxes", return_value=[]):
        content = build_html()
    assert "Jane Doe" in content
    assert "this is a failed partnership" in content
    assert "Critical" in content


def test_build_html_escapes_html_in_quotes():
    flag_rows = [
        {"id": 1, "customer_name": "<script>alert(1)</script>", "address": "x", "pm_name": "y",
         "severity": "High", "first_detected": "Oct 01, 2026", "issue_summary": "x",
         "evidence_quote": "<b>not bold</b>", "source": "call, outbound"},
    ]
    with patch("casey_flag_review._critical_high_flags", return_value=flag_rows), \
         patch("casey_flag_review._top_cost_jobs", return_value=[]), \
         patch("casey_flag_review._failed_mailboxes", return_value=[]):
        content = build_html()
    assert "<script>" not in content
    assert "&lt;script&gt;" in content


def test_build_html_shows_none_failed_when_all_mailboxes_ok():
    with patch("casey_flag_review._critical_high_flags", return_value=[]), \
         patch("casey_flag_review._top_cost_jobs", return_value=[]), \
         patch("casey_flag_review._failed_mailboxes", return_value=[]):
        content = build_html()
    assert "None" in content


def test_build_html_lists_failed_mailboxes():
    with patch("casey_flag_review._critical_high_flags", return_value=[]), \
         patch("casey_flag_review._top_cost_jobs", return_value=[]), \
         patch("casey_flag_review._failed_mailboxes", return_value=[
             {"pm_name": "Gustavo", "email": "gustavo@example.com", "error": "403 forbidden"}]):
        content = build_html()
    assert "gustavo@example.com" in content
    assert "403 forbidden" in content


def test_build_html_lists_top_cost_jobs():
    with patch("casey_flag_review._critical_high_flags", return_value=[]), \
         patch("casey_flag_review._top_cost_jobs", return_value=[
             {"job_key": "Acme|to_start|Deck", "pm_name": "Gustavo", "analyzed_count": 12}]), \
         patch("casey_flag_review._failed_mailboxes", return_value=[]):
        content = build_html()
    assert "Acme|to_start|Deck" in content
    assert "12" in content
