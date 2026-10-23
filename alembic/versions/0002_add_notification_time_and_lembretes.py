"""0002_add_notification_time_and_lembretes

Revision ID: 0002_add_notification_time_and_lembretes
Revises: 0001_initial
Create Date: 2026-10-23 07:15:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002_add_notification_time_and_lembretes'
down_revision: Union[str, None] = '0001_initial'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Adicionar horario_notificacao_padrao na tabela proprietarios
    op.add_column(
        'proprietarios',
        sa.Column('horario_notificacao_padrao', sa.Time(), nullable=False, server_default='09:00:00')
    )

    # 2. Adicionar data_hora_agendada e status na tabela manutencoes
    op.add_column(
        'manutencoes',
        sa.Column('data_hora_agendada', sa.DateTime(), nullable=True)
    )
    op.add_column(
        'manutencoes',
        sa.Column('status', sa.String(length=30), nullable=False, server_default='concluido')
    )

    # 3. Criar tabela lembretes
    op.create_table(
        'lembretes',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('veiculo_id', sa.Integer(), nullable=False),
        sa.Column('descricao', sa.String(length=255), nullable=False),
        sa.Column('data_hora_agendada', sa.DateTime(), nullable=True),
        sa.Column('odometro_alvo', sa.Integer(), nullable=True),
        sa.Column('status', sa.String(length=30), nullable=False, server_default='pendente'),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['veiculo_id'], ['veiculos.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_lembretes_veiculo_id'), 'lembretes', ['veiculo_id'], unique=False)
    op.create_index(op.f('ix_lembretes_status'), 'lembretes', ['status'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_lembretes_status'), table_name='lembretes')
    op.drop_index(op.f('ix_lembretes_veiculo_id'), table_name='lembretes')
    op.drop_table('lembretes')
    op.drop_column('manutencoes', 'status')
    op.drop_column('manutencoes', 'data_hora_agendada')
    op.drop_column('proprietarios', 'horario_notificacao_padrao')
