"""Machines: vm_id (a virtual machine on the Proxmox host for working on images)

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-09 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0026'
down_revision: Union[str, Sequence[str], None] = '0025'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Plain ADD COLUMN and a unique index: nothing to rebuild (see 0021).
    op.add_column('machines', sa.Column('vm_id', sa.Integer(), nullable=True))
    op.create_index('ix_machines_vm_id', 'machines', ['vm_id'], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_machines_vm_id', table_name='machines')
    with op.batch_alter_table('machines') as b:
        b.drop_column('vm_id')
