"""The Background tasks panel's list (app.api.v1.tasks)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import sessionmaker

from app.db.models import BackupJob, Image, ImageKind
from app.db.session import make_engine


def test_running_and_recent_jobs_are_listed_with_their_progress(client, db_url):
    engine = make_engine(db_url)
    now = datetime.now(timezone.utc)
    with sessionmaker(bind=engine)() as db:
        db.add_all([
            BackupJob(kind="backup", image_name="hw-test", location="usb:sdj1", location_label="USB Ultra (vfat)",
                      status="running", done_bytes=3 << 30, total_bytes=12 << 30, started_at=now),
            BackupJob(kind="restore", image_name="cs2-b", location="usb:sdj1", status="done",
                      done_bytes=5, total_bytes=5, finished_at=now - timedelta(minutes=2)),
            BackupJob(kind="backup", image_name="old", location="usb:sdj1", status="done",
                      finished_at=now - timedelta(hours=3)),                       # long done: not listed
            Image(name="win11", zvol_path="tank/ggnet/images/win11", size_gb=80, kind=ImageKind.OS,
                  copy_source="/imports/win11.vhdx", copy_percent=42),
            Image(name="cs2-copy", zvol_path="tank/ggnet/images/cs2-copy", size_gb=10,
                  copy_source="tank/ggnet/images/cs2@v3", copy_percent=7),
            Image(name="restoring", zvol_path="tank/ggnet/images/restoring", size_gb=10,
                  copy_source="backup:/run/ggnet/usb/sdj1/ggnet-backups/x-20261107-120000", copy_percent=1),
        ])
        db.commit()
    engine.dispose()
    tasks = client.get("/api/v1/tasks").json()
    shown = [(t["kind"], t["title"], t["status"], t["percent"], t["cancel"]) for t in tasks]
    assert shown == [
        ("backup", "Backup of hw-test", "running", 25, "/api/v1/backups/jobs/1/cancel"),
        ("restore", "Restore of cs2-b", "done", 100, None),
        ("import", "Import into win11", "running", 42, "/api/v1/imports/1/cancel"),
        ("copy", "Copy into cs2-copy", "running", 7, None),
    ]
    assert tasks[0]["detail"] == "USB Ultra (vfat)" and tasks[2]["detail"] == "win11.vhdx"
