"""initial schema: users, sessions, documents (files, extraction, sheet, versions, exports) and the audit log

Revision ID: 0001
Revises: 
Create Date: 2026-10-19 23:27:37.571272
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql


revision: str = '0001'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('email', sa.String(length=255), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('nimbus_user_id', sa.String(length=64), nullable=True),
    sa.Column('nimbus_role', sa.String(length=64), nullable=True),
    sa.Column('role', sa.String(length=32), nullable=False),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_login_at', sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('email', name=op.f('uq_users_email'))
    )
    op.create_table('documents',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=255), nullable=False),
    sa.Column('invoice_no', sa.String(length=100), nullable=True),
    sa.Column('invoice_date', sa.String(length=40), nullable=True),
    sa.Column('customer_id', sa.String(length=100), nullable=True),
    sa.Column('template_id', sa.String(length=100), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('needs_review', sa.Boolean(), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('line_count', sa.Integer(), nullable=False),
    sa.Column('options', sa.JSON(), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_by', sa.Integer(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('confirmed_by', sa.Integer(), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('last_exported_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('export_count', sa.Integer(), nullable=False),
    sa.ForeignKeyConstraint(['confirmed_by'], ['users.id'], name=op.f('fk_documents_confirmed_by_users')),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_documents_created_by_users')),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_documents_updated_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_documents'))
    )
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_documents_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_documents_created_by'), ['created_by'], unique=False)
        batch_op.create_index(batch_op.f('ix_documents_invoice_no'), ['invoice_no'], unique=False)
        batch_op.create_index(batch_op.f('ix_documents_status'), ['status'], unique=False)
        batch_op.create_index('ix_documents_status_updated', ['status', 'updated_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_documents_updated_at'), ['updated_at'], unique=False)

    op.create_table('user_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('ip', sa.String(length=64), nullable=True),
    sa.Column('user_agent', sa.String(length=255), nullable=True),
    sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_user_sessions_token_hash'))
    )
    with op.batch_alter_table('user_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_sessions_user_id'), ['user_id'], unique=False)

    op.create_table('audit_logs',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.String(length=36), nullable=True),
    sa.Column('user_id', sa.Integer(), nullable=True),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('detail', sa.JSON(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_audit_logs_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_audit_logs_user_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_logs'))
    )
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_audit_logs_action'), ['action'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_logs_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_logs_document_id'), ['document_id'], unique=False)

    op.create_table('document_exports',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.String(length=36), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('format', sa.String(length=10), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('row_count', sa.Integer(), nullable=False),
    sa.Column('final', sa.Boolean(), nullable=False),
    sa.Column('exported_by', sa.Integer(), nullable=False),
    sa.Column('exported_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_exports_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['exported_by'], ['users.id'], name=op.f('fk_document_exports_exported_by_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_document_exports'))
    )
    with op.batch_alter_table('document_exports', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_document_exports_document_id'), ['document_id'], unique=False)

    op.create_table('document_extractions',
    sa.Column('document_id', sa.String(length=36), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('engine_version', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_extractions_document_id_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('document_id', name=op.f('pk_document_extractions'))
    )
    op.create_table('document_files',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.String(length=36), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('filename', sa.String(length=255), nullable=False),
    sa.Column('content_type', sa.String(length=100), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False),
    sa.Column('storage', sa.String(length=20), nullable=False),
    sa.Column('storage_key', sa.String(length=500), nullable=True),
    sa.Column('data', sa.LargeBinary().with_variant(mysql.LONGBLOB(), 'mysql'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_files_document_id_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_document_files'))
    )
    with op.batch_alter_table('document_files', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_document_files_document_id'), ['document_id'], unique=False)

    op.create_table('document_sheets',
    sa.Column('document_id', sa.String(length=36), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('rows', sa.JSON(), nullable=False),
    sa.Column('flags', sa.JSON(), nullable=False),
    sa.Column('updated_by', sa.Integer(), nullable=True),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_document_sheets_document_id_documents'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['updated_by'], ['users.id'], name=op.f('fk_document_sheets_updated_by_users')),
    sa.PrimaryKeyConstraint('document_id', name=op.f('pk_document_sheets'))
    )
    op.create_table('sheet_versions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('document_id', sa.String(length=36), nullable=False),
    sa.Column('revision', sa.Integer(), nullable=False),
    sa.Column('rows', sa.JSON(), nullable=False),
    sa.Column('reason', sa.String(length=20), nullable=False),
    sa.Column('created_by', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['created_by'], ['users.id'], name=op.f('fk_sheet_versions_created_by_users')),
    sa.ForeignKeyConstraint(['document_id'], ['documents.id'], name=op.f('fk_sheet_versions_document_id_documents'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_sheet_versions'))
    )
    with op.batch_alter_table('sheet_versions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sheet_versions_document_id'), ['document_id'], unique=False)



def downgrade() -> None:
    with op.batch_alter_table('sheet_versions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sheet_versions_document_id'))

    op.drop_table('sheet_versions')
    op.drop_table('document_sheets')
    with op.batch_alter_table('document_files', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_document_files_document_id'))

    op.drop_table('document_files')
    op.drop_table('document_extractions')
    with op.batch_alter_table('document_exports', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_document_exports_document_id'))

    op.drop_table('document_exports')
    with op.batch_alter_table('audit_logs', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_audit_logs_document_id'))
        batch_op.drop_index(batch_op.f('ix_audit_logs_created_at'))
        batch_op.drop_index(batch_op.f('ix_audit_logs_action'))

    op.drop_table('audit_logs')
    with op.batch_alter_table('user_sessions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_user_sessions_user_id'))

    op.drop_table('user_sessions')
    with op.batch_alter_table('documents', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_documents_updated_at'))
        batch_op.drop_index('ix_documents_status_updated')
        batch_op.drop_index(batch_op.f('ix_documents_status'))
        batch_op.drop_index(batch_op.f('ix_documents_invoice_no'))
        batch_op.drop_index(batch_op.f('ix_documents_created_by'))
        batch_op.drop_index(batch_op.f('ix_documents_created_at'))

    op.drop_table('documents')
    op.drop_table('users')
