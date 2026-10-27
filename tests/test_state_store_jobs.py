from db.state_store import upsert_active_job, get_all_active_jobs, _memory_jobs


def _job(client_name="Test Client", sheet_tab="to_start", primary_job_type="Deck", **overrides):
    job = {
        "client_name": client_name,
        "sheet_tab": sheet_tab,
        "primary_job_type": primary_job_type,
        "job_type": primary_job_type,
        "pm_name": "",
    }
    job.update(overrides)
    return job


def test_address_is_saved_on_insert():
    key = ("Address Insert Client", "to_start", "Deck")
    _memory_jobs.pop(key, None)
    try:
        upsert_active_job(_job(client_name="Address Insert Client", address="123 Main St"))
        rows = get_all_active_jobs()
        row = next(r for r in rows if r["client_name"] == "Address Insert Client")
        assert row["address"] == "123 Main St"
    finally:
        _memory_jobs.pop(key, None)


def test_address_is_updated_on_existing_row():
    key = ("Address Update Client", "to_start", "Deck")
    _memory_jobs.pop(key, None)
    try:
        upsert_active_job(_job(client_name="Address Update Client", address="Old Address"))
        upsert_active_job(_job(client_name="Address Update Client", address="New Address"))
        rows = get_all_active_jobs()
        row = next(r for r in rows if r["client_name"] == "Address Update Client")
        assert row["address"] == "New Address"
    finally:
        _memory_jobs.pop(key, None)
