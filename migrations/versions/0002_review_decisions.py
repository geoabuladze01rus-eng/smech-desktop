"""Append-only engineer/manager snapshot review decisions.

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('smech_review_decisions',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('project_id', sa.String(100), sa.ForeignKey('smech_projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('snapshot_id', sa.String(36), sa.ForeignKey('smech_snapshots.id', ondelete='CASCADE'), nullable=False),
        sa.Column('actor_id', sa.String(36), sa.ForeignKey('smech_users.id'), nullable=False),
        sa.Column('stage', sa.String(24), nullable=False),
        sa.Column('decision', sa.String(16), nullable=False),
        sa.Column('note', sa.String(1000), nullable=False),
        sa.Column('snapshot_hash', sa.String(64), nullable=False),
        sa.Column('at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('snapshot_id', 'stage', name='uq_smech_review_stage'))
    op.create_index('ix_smech_review_decisions_project_id', 'smech_review_decisions', ['project_id'])
    op.create_index('ix_smech_review_decisions_snapshot_id', 'smech_review_decisions', ['snapshot_id'])


def downgrade():
    op.drop_index('ix_smech_review_decisions_snapshot_id', table_name='smech_review_decisions')
    op.drop_index('ix_smech_review_decisions_project_id', table_name='smech_review_decisions')
    op.drop_table('smech_review_decisions')
