"""0001_initial

Revision ID: 0001_initial
Revises: 
Create Date: 2026-10-22 17:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Tabela proprietarios
    op.create_table(
        'proprietarios',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('nome', sa.String(length=100), nullable=False),
        sa.Column('email', sa.String(length=120), nullable=False),
        sa.Column('telefone', sa.String(length=20), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_proprietarios_email'), 'proprietarios', ['email'], unique=True)

    # 2. Tabela veiculos
    op.create_table(
        'veiculos',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('placa', sa.String(length=10), nullable=False),
        sa.Column('marca', sa.String(length=50), nullable=False),
        sa.Column('modelo', sa.String(length=50), nullable=False),
        sa.Column('ano', sa.Integer(), nullable=False),
        sa.Column('quilometragem', sa.Integer(), nullable=False),
        sa.Column('proprietario_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['proprietario_id'], ['proprietarios.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_veiculos_placa'), 'veiculos', ['placa'], unique=True)
    op.create_index(op.f('ix_veiculos_proprietario_id'), 'veiculos', ['proprietario_id'], unique=False)

    # 3. Tabela manutencoes
    op.create_table(
        'manutencoes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('veiculo_id', sa.Integer(), nullable=False),
        sa.Column('descricao', sa.String(length=255), nullable=False),
        sa.Column('custo', sa.Float(), nullable=False),
        sa.Column('data', sa.Date(), nullable=False),
        sa.Column('odometro_registro', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['veiculo_id'], ['veiculos.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_manutencoes_veiculo_id'), 'manutencoes', ['veiculo_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_manutencoes_veiculo_id'), table_name='manutencoes')
    op.drop_table('manutencoes')
    op.drop_index(op.f('ix_veiculos_proprietario_id'), table_name='veiculos')
    op.drop_index(op.f('ix_veiculos_placa'), table_name='veiculos')
    op.drop_table('veiculos')
    op.drop_index(op.f('ix_proprietarios_email'), table_name='proprietarios')
    op.drop_table('proprietarios')
