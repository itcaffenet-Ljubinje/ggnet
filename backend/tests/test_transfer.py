"""copy-database: every row of an old SQLite database into the configured (empty) one."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from alembic import command
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import AppSetting, Image, ImageKind, Machine, MachineMode
from app.db.session import make_engine
from app.db.transfer import TransferError, copy_database

from .conftest import alembic_config


@pytest.fixture
def source(tmp_path):
    url = f"sqlite:///{tmp_path / 'old.db'}"
    command.upgrade(alembic_config(url), "head")
    engine = make_engine(url)
    with Session(engine) as db:
        game = Image(name="games", zvol_path="tank/ggnet/images/games", size_gb=100, kind=ImageKind.GAME)
        db.add(game)
        db.flush()
        for n in range(1, 4):
            db.add(Machine(name=f"pc{n:02d}", mode=MachineMode.DISK, initiator_iqn=f"iqn.x:pc{n}",
                           game_image_id=game.id, last_seen_at=datetime(2026, 11, 8, 21, 0, tzinfo=timezone.utc)))
        db.add(AppSetting(key="agent", value='{"auto_update": false}'))
        db.commit()
    yield engine
    engine.dispose()


def test_copies_every_row_and_new_ids_follow(source, db_url):
    target = make_engine(db_url)
    copied = {c.table: c.rows for c in copy_database(source, target)}
    assert (copied["images"], copied["machines"], copied["settings"]) == (1, 3, 1)

    with Session(target) as db:
        pcs = db.scalars(select(Machine).order_by(Machine.name)).all()
        assert [m.name for m in pcs] == ["pc01", "pc02", "pc03"]
        assert pcs[0].game_image.name == "games"
        assert pcs[0].last_seen_at == datetime(2026, 11, 8, 21, 0, tzinfo=timezone.utc)
        assert db.get(AppSetting, "agent").value == '{"auto_update": false}'
        # The next machine gets a new id (PostgreSQL: the sequence continues after the copy).
        db.add(Machine(name="pc04", mode=MachineMode.DISK, initiator_iqn="iqn.x:pc4"))
        db.commit()
        assert db.scalar(select(func.max(Machine.id))) == 4
    target.dispose()


def test_refuses_a_target_that_has_data(source, db_url):
    target = make_engine(db_url)
    copy_database(source, target)
    with pytest.raises(TransferError, match="not empty"):
        copy_database(source, target)
    target.dispose()


def test_refuses_databases_at_different_migrations(source, tmp_path):
    url = f"sqlite:///{tmp_path / 'older.db'}"
    command.upgrade(alembic_config(url), "0020")
    target = make_engine(url)
    with pytest.raises(TransferError, match="different migrations"):
        copy_database(source, target)
    target.dispose()
