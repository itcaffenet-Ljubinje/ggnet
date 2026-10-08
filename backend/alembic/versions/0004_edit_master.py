"""edit master: machine that fills a draft game disk

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-08 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0004'
down_revision: Union[str, Sequence[str], None] = '0003'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.add_column(sa.Column('editing_disk_id', sa.Integer(), nullable=True))
        batch_op.create_unique_constraint(batch_op.f('uq_machines_editing_disk_id'), ['editing_disk_id'])
        batch_op.create_foreign_key(batch_op.f('fk_machines_editing_disk_id_game_disks'), 'game_disks',
                                    ['editing_disk_id'], ['id'], ondelete='RESTRICT')


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('machines', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_machines_editing_disk_id_game_disks'), type_='foreignkey')
        batch_op.drop_constraint(batch_op.f('uq_machines_editing_disk_id'), type_='unique')
        batch_op.drop_column('editing_disk_id')
