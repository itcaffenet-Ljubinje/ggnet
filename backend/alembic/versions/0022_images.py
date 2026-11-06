"""Images: game_disks becomes images; PCs and cards point to image ids

OS images and game images share one table; its old name said "game disk".
Only names change, every row stays. SQLite cannot rename a constraint or an
index, so `images` and `machines` are rebuilt (batch mode) with foreign key
checks off for the migration: dropping a table that others point to is
refused otherwise (see 0021). The foreign keys are checked before they are
turned on again.

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-06 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = '0022'
down_revision: Union[str, Sequence[str], None] = '0021'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (old column, new column, indexed or unique) on machines; all point to the image table.
MACHINE_COLUMNS = (
    ('game_disk_id', 'game_image_id', 'index'),
    ('os_disk_id', 'os_image_id', 'index'),
    ('editing_disk_id', 'editing_image_id', 'unique'),
)


def _foreign_keys(on: bool) -> None:
    """PRAGMA foreign_keys only works outside a transaction."""
    if op.get_bind().dialect.name != 'sqlite':
        return
    if on:
        broken = op.get_bind().exec_driver_sql('PRAGMA foreign_key_check').fetchall()
        if broken:
            raise RuntimeError(f'foreign keys broken by the migration: {broken}')
    with op.get_context().autocommit_block():
        op.execute(f"PRAGMA foreign_keys={'ON' if on else 'OFF'}")


def _rename(old_table: str, new_table: str, columns: list[tuple[str, str, str]], card: tuple[str, str]) -> None:
    """Rename the image table, its constraints, the machines' columns and the cards' column."""
    op.rename_table(old_table, new_table)
    with op.batch_alter_table(new_table, recreate='always') as b:
        for col in ('name', 'zvol_path'):
            b.drop_constraint(f'uq_{old_table}_{col}', type_='unique')
            b.create_unique_constraint(f'uq_{new_table}_{col}', [col])
    # Two passes: within one batch the new column names are not known yet.
    with op.batch_alter_table('machines', recreate='always') as b:
        for old_col, new_col, kind in columns:
            if kind == 'index':
                b.drop_index(f'ix_machines_{old_col}')
            else:
                b.drop_constraint(f'uq_machines_{old_col}', type_='unique')
            b.drop_constraint(f'fk_machines_{old_col}_{old_table}', type_='foreignkey')
            b.alter_column(old_col, new_column_name=new_col)
    with op.batch_alter_table('machines', recreate='always') as b:
        for _, new_col, kind in columns:
            if kind == 'index':
                b.create_index(f'ix_machines_{new_col}', [new_col], unique=False)
            else:
                b.create_unique_constraint(f'uq_machines_{new_col}', [new_col])
            b.create_foreign_key(f'fk_machines_{new_col}_{new_table}', new_table, [new_col], ['id'],
                                 ondelete='RESTRICT')
    op.execute(f'ALTER TABLE image_hardware RENAME COLUMN {card[0]} TO {card[1]}')


def upgrade() -> None:
    """Upgrade schema."""
    _foreign_keys(False)
    _rename('game_disks', 'images', list(MACHINE_COLUMNS), ('disk_id', 'image_id'))
    _foreign_keys(True)


def downgrade() -> None:
    """Downgrade schema."""
    _foreign_keys(False)
    _rename('images', 'game_disks', [(new, old, kind) for old, new, kind in MACHINE_COLUMNS], ('image_id', 'disk_id'))
    _foreign_keys(True)
