from agents.oca.main import enrich_comms_hold


def _sheet_job(client_name, sheet_tab, primary_job_type, comms_hold, reason=""):
    return {
        "client_name": client_name,
        "sheet_tab": sheet_tab,
        "primary_job_type": primary_job_type,
        "comms_hold": comms_hold,
        "comms_hold_reason": reason,
    }


def _db_row(client_name, sheet_tab, primary_job_type, comms_hold, reason=""):
    return {
        "client_name": client_name,
        "sheet_tab": sheet_tab,
        "primary_job_type": primary_job_type,
        "comms_hold": comms_hold,
        "comms_hold_reason": reason,
    }


def test_two_jobs_same_client_one_hold_one_normal_do_not_collide():
    """Reproduces the reported bug: a client with two jobs (different
    primary_job_type) — one on HOLD, one normal — must keep each job's
    own comms_hold after enrichment, not have one leak onto the other."""
    sheet_jobs = [
        _sheet_job("George Dimov", "in_process", "Renovations", comms_hold=True, reason="Sheet PM cell: 'HOLD/Laura'"),
        _sheet_job("George Dimov", "in_process", "Installation", comms_hold=False),
    ]
    db_rows = [
        _db_row("George Dimov", "in_process", "Renovations", comms_hold=True, reason="Sheet PM cell: 'HOLD/Laura'"),
        _db_row("George Dimov", "in_process", "Installation", comms_hold=False),
    ]

    enrich_comms_hold(sheet_jobs, db_rows)

    renovations = next(j for j in sheet_jobs if j["primary_job_type"] == "Renovations")
    installation = next(j for j in sheet_jobs if j["primary_job_type"] == "Installation")
    assert renovations["comms_hold"] is True
    assert installation["comms_hold"] is False


def test_three_jobs_same_client_each_keeps_its_own_hold_status():
    """Reproduces the Caroline Day case: 2 held To Start rows (duplicate
    job type, different sheet_tab doesn't apply here — same tab/type is
    a separate, reported-not-fixed issue) plus 1 normal In Process row."""
    sheet_jobs = [
        _sheet_job("Caroline Day", "to_start", "Landscaping", comms_hold=True),
        _sheet_job("Caroline Day", "in_process", "Fence", comms_hold=False),
    ]
    db_rows = [
        _db_row("Caroline Day", "to_start", "Landscaping", comms_hold=True),
        _db_row("Caroline Day", "in_process", "Fence", comms_hold=False),
    ]

    enrich_comms_hold(sheet_jobs, db_rows)

    landscaping = next(j for j in sheet_jobs if j["primary_job_type"] == "Landscaping")
    fence = next(j for j in sheet_jobs if j["primary_job_type"] == "Fence")
    assert landscaping["comms_hold"] is True
    assert fence["comms_hold"] is False  # must NOT inherit the hold from the other job


def test_order_of_db_rows_does_not_affect_result():
    """The bug was order-dependent (dict comprehension, last write wins) —
    confirm the fix is order-independent regardless of DB row order."""
    sheet_jobs = [
        _sheet_job("X", "in_process", "A", comms_hold=False),
        _sheet_job("X", "in_process", "B", comms_hold=True),
    ]
    db_rows_order1 = [
        _db_row("X", "in_process", "A", comms_hold=False),
        _db_row("X", "in_process", "B", comms_hold=True),
    ]
    db_rows_order2 = list(reversed(db_rows_order1))

    jobs1 = [dict(j) for j in sheet_jobs]
    jobs2 = [dict(j) for j in sheet_jobs]
    enrich_comms_hold(jobs1, db_rows_order1)
    enrich_comms_hold(jobs2, db_rows_order2)

    assert jobs1 == jobs2
    a1 = next(j for j in jobs1 if j["primary_job_type"] == "A")
    b1 = next(j for j in jobs1 if j["primary_job_type"] == "B")
    assert a1["comms_hold"] is False
    assert b1["comms_hold"] is True


def test_no_matching_db_row_defaults_to_not_held():
    sheet_jobs = [_sheet_job("New Client", "to_start", "Deck", comms_hold=False)]
    enrich_comms_hold(sheet_jobs, db_rows=[])
    assert sheet_jobs[0]["comms_hold"] is False
    assert sheet_jobs[0]["comms_hold_reason"] == ""
