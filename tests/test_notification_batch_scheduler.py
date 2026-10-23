"""
AutoCare V2 - Testes Automatizados do Motor de Notificações, Batching e Scheduler
Validação rigorosa dos 3 problemas críticos de UX:
1. Resolução do Disparo Precoce (05:00 vs 08:00):
   Lembretes agendados para horários futuros são ignorados pelo boletim diário e disparam no minuto exato.
2. Agrupamento em Lote (Batching) & Isolamento Estrito por Chave Estrangeira (FK):
   Um único boletim elegante emitido por condutor, com alertas de veículos isolados (Civic nunca mistura com Sandero).
3. Horário Personalizado do Check-up Diário:
   Respeito estrito ao horario_notificacao_padrao de cada proprietário.
"""

import sys
import uuid
import random
import string
import pytest
from datetime import datetime, date, time, timedelta

def random_mercosul_plate() -> str:
    letters1 = "".join(random.choices(string.ascii_uppercase, k=3))
    digit1 = random.choice(string.digits)
    letter2 = random.choice(string.ascii_uppercase)
    digits2 = "".join(random.choices(string.digits, k=2))
    return f"{letters1}{digit1}{letter2}{digits2}"

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.infrastructure.database.session import init_db
from src.database import async_session_factory
from src.models.proprietario import Proprietario
from src.models.veiculo import Veiculo
from src.models.lembrete import Lembrete
from src.repositories.proprietario_repository import ProprietarioRepository
from src.repositories.veiculo_repository import VeiculoRepository
from src.repositories.lembrete_repository import LembreteRepository
from src.schemas.proprietario import ProprietarioCreate
from src.schemas.veiculo import VeiculoCreate
from src.schemas.lembrete import LembreteCreate
from src.application.services.notification_service import NotificationService


@pytest.fixture(autouse=True)
async def setup_db():
    await init_db()


@pytest.mark.asyncio
async def test_problema_1_lembrete_futuro_ignorado_as_05h_e_disparado_as_08h():
    """
    PROBLEMA 1: O sistema não deve disparar às 05:00 um agendamento do usuário para as 08:00.
    """
    async with async_session_factory() as session:
        prop_repo = ProprietarioRepository(session)
        veic_repo = VeiculoRepository(session)
        lemb_repo = LembreteRepository(session)

        uid = uuid.uuid4().hex[:6]
        # 1. Cria proprietário com horário padrão 05:00
        prop = await prop_repo.create(
            ProprietarioCreate(
                nome="Carlos Horários",
                email=f"carlos_{uid}@autocare.com.br",
                telefone=f"551198888{uid[:4]}",
                horario_notificacao_padrao=time(5, 0),
            )
        )

        # 2. Cria veículo
        veiculo = await veic_repo.create(
            VeiculoCreate(
                placa=random_mercosul_plate(),
                marca="Honda",
                modelo="Civic",
                ano=2020,
                quilometragem=50000,
                proprietario_id=prop.id,
            )
        )

        # 3. Cria lembrete agendado especificamente para as 08:00 de hoje
        hoje = date.today()
        dt_05h = datetime.combine(hoje, time(5, 0))
        dt_08h = datetime.combine(hoje, time(8, 0))

        lembrete_08h = await lemb_repo.create(
            LembreteCreate(
                veiculo_id=veiculo.id,
                descricao="Revisão das pastilhas de freio",
                data_hora_agendada=dt_08h,
                status="agendado",
            )
        )

        service = NotificationService()

        # SIMULAÇÃO: Execução às 05:00 da manhã
        # O boletim geral NÃO deve conter este lembrete pois seu agendamento é futuro (08:00)
        bulletin_05h = await service.build_garage_bulletin(session, prop.id, current_dt=dt_05h)
        assert bulletin_05h is None, "Lembrete das 08:00 NÃO pode aparecer no boletim das 05:00!"

        # A rotina de lembretes pontuais às 05:00 também não deve disparar
        due_05h = await service.dispatch_scheduled_reminders(session, current_dt=dt_05h)
        due_05h_prop = [d for d in due_05h if d["proprietario_id"] == prop.id]
        assert len(due_05h_prop) == 0, "Nenhum lembrete pontual deveria ser disparado às 05:00!"

        # SIMULAÇÃO: Chegada das 08:00 da manhã
        due_08h = await service.dispatch_scheduled_reminders(session, current_dt=dt_08h)
        due_08h_prop = [d for d in due_08h if d["proprietario_id"] == prop.id]
        assert len(due_08h_prop) == 1, "Às 08:00 o lembrete específico DEVE disparar de forma isolada!"
        assert due_08h_prop[0]["lembrete_id"] == lembrete_08h.id
        assert "Civic" in due_08h_prop[0]["mensagem"]
        assert "Revisão das pastilhas" in due_08h_prop[0]["mensagem"]

        # Verifica se foi marcado como concluído no banco
        lem_atualizado = await lemb_repo.get_by_id(lembrete_08h.id)
        assert lem_atualizado.status == "concluido"


@pytest.mark.asyncio
async def test_problema_2_batching_resumo_unico_e_isolamento_estrito_fk():
    """
    PROBLEMA 2: Agrupamento em lote (Batching) sem spam e com isolamento estrito de FK.
    Civic e Sandero devem ser consolidados em uma única mensagem elegante.
    O alerta do Civic não pode estar no bloco do Sandero e vice-versa.
    """
    async with async_session_factory() as session:
        prop_repo = ProprietarioRepository(session)
        veic_repo = VeiculoRepository(session)
        lemb_repo = LembreteRepository(session)

        uid = uuid.uuid4().hex[:6]
        # 1. Proprietário com dois carros
        prop = await prop_repo.create(
            ProprietarioCreate(
                nome="Mariana Frota",
                email=f"mariana_{uid}@autocare.com.br",
                telefone=f"551198888{uid[:4]}",
                horario_notificacao_padrao=time(9, 0),
            )
        )

        civic = await veic_repo.create(
            VeiculoCreate(
                placa=random_mercosul_plate(),
                marca="Honda",
                modelo="Civic",
                ano=2021,
                quilometragem=45000,
                proprietario_id=prop.id,
            )
        )

        sandero = await veic_repo.create(
            VeiculoCreate(
                placa=random_mercosul_plate(),
                marca="Renault",
                modelo="Sandero",
                ano=2019,
                quilometragem=72000,
                proprietario_id=prop.id,
            )
        )

        # 2. Lembretes associados estritamente por veiculo_id (FK)
        # Civic: Óleo atrasado
        await lemb_repo.create(
            LembreteCreate(
                veiculo_id=civic.id,
                descricao="Óleo atrasado",
                status="pendente",
            )
        )

        # Sandero: Calibrar pneus
        await lemb_repo.create(
            LembreteCreate(
                veiculo_id=sandero.id,
                descricao="Calibrar pneus",
                status="pendente",
            )
        )

        service = NotificationService()
        agora = datetime.combine(date.today(), time(9, 0))

        # 3. Gera boletim consolidado
        bulletin = await service.build_garage_bulletin(session, prop.id, current_dt=agora)

        assert bulletin is not None
        # Verifica saudação elegante
        assert "Bom dia, Mariana! Que tal verificar sua garagem hoje?" in bulletin

        # Separa as linhas do boletim para verificação estrita por veículo
        linhas = [line.strip() for line in bulletin.split("\n") if line.strip()]

        # Linha do Civic
        linha_civic = next((l for l in linhas if "Civic" in l), None)
        assert linha_civic is not None, "Civic deve estar presente no boletim"
        assert "Óleo atrasado" in linha_civic, "Óleo deve estar associado ao Civic"
        assert "Calibrar pneus" not in linha_civic, "Calibrar pneus JAMAIS pode vazar para o Civic!"

        # Linha do Sandero
        linha_sandero = next((l for l in linhas if "Sandero" in l), None)
        assert linha_sandero is not None, "Sandero deve estar presente no boletim"
        assert "Calibrar pneus" in linha_sandero, "Calibrar pneus deve estar associado ao Sandero"
        assert "Óleo atrasado" not in linha_sandero, "Óleo atrasado JAMAIS pode vazar para o Sandero!"

        # 4. Despacho do Boletim Diário às 09:00:
        # Garante que é enviada APENAS UMA MENSAGEM (eliminando spam de múltiplas notificações)
        disparos = await service.dispatch_daily_bulletins(session, target_time=time(9, 0), current_dt=agora)
        disparos_mariana = [d for d in disparos if d["proprietario_id"] == prop.id]

        assert len(disparos_mariana) == 1, "Deve enviar exatamente 1 boletim consolidado, sem spam!"
        assert disparos_mariana[0]["bulletin"] == bulletin


@pytest.mark.asyncio
async def test_problema_3_horario_personalizado_checkup_diario():
    """
    PROBLEMA 3: O sistema deve permitir configurar e respeitar o horário preferido do usuário.
    Às 09:00 só dispara para quem marcou 09:00. Às 14:30 dispara para quem marcou 14:30.
    """
    async with async_session_factory() as session:
        prop_repo = ProprietarioRepository(session)
        veic_repo = VeiculoRepository(session)
        lemb_repo = LembreteRepository(session)

        uid_a = uuid.uuid4().hex[:6]
        uid_b = uuid.uuid4().hex[:6]

        # Usuário A: prefere 09:00
        prop_manha = await prop_repo.create(
            ProprietarioCreate(
                nome="Arthur Matutino",
                email=f"arthur_{uid_a}@autocare.com.br",
                telefone=f"551197777{uid_a[:4]}",
                horario_notificacao_padrao=time(9, 0),
            )
        )
        v_a = await veic_repo.create(
            VeiculoCreate(
                placa=random_mercosul_plate(),
                marca="Toyota",
                modelo="Corolla",
                ano=2022,
                quilometragem=30000,
                proprietario_id=prop_manha.id,
            )
        )
        await lemb_repo.create(
            LembreteCreate(
                veiculo_id=v_a.id,
                descricao="Rodízio de pneus",
                status="pendente",
            )
        )

        # Usuário B: prefere 14:30
        prop_tarde = await prop_repo.create(
            ProprietarioCreate(
                nome="Beatriz Vespertina",
                email=f"beatriz_{uid_b}@autocare.com.br",
                telefone=f"551197777{uid_b[:4]}",
                horario_notificacao_padrao=time(14, 30),
            )
        )
        v_b = await veic_repo.create(
            VeiculoCreate(
                placa=random_mercosul_plate(),
                marca="Volkswagen",
                modelo="Polo",
                ano=2023,
                quilometragem=15000,
                proprietario_id=prop_tarde.id,
            )
        )
        await lemb_repo.create(
            LembreteCreate(
                veiculo_id=v_b.id,
                descricao="Troca do filtro de cabine",
                status="pendente",
            )
        )

        service = NotificationService()

        # TESTE 3.1: Ciclo das 09:00
        agora_09h = datetime.combine(date.today(), time(9, 0))
        disparos_09h = await service.dispatch_daily_bulletins(session, target_time=time(9, 0), current_dt=agora_09h)
        ids_09h = [d["proprietario_id"] for d in disparos_09h]

        assert prop_manha.id in ids_09h, "Arthur deve receber o boletim às 09:00"
        assert prop_tarde.id not in ids_09h, "Beatriz NÃO pode receber o boletim às 09:00 (seu horário é 14:30)"

        # TESTE 3.2: Ciclo das 14:30
        agora_1430 = datetime.combine(date.today(), time(14, 30))
        disparos_1430 = await service.dispatch_daily_bulletins(session, target_time=time(14, 30), current_dt=agora_1430)
        ids_1430 = [d["proprietario_id"] for d in disparos_1430]

        assert prop_tarde.id in ids_1430, "Beatriz deve receber o boletim às 14:30"
        assert prop_manha.id not in ids_1430, "Arthur NÃO deve receber outro boletim às 14:30"

        # TESTE 3.3: Atualização do horário preferido
        await prop_repo.update_horario_notificacao(prop_manha.id, time(19, 0))
        prop_atualizado = await prop_repo.get_by_id(prop_manha.id)
        assert prop_atualizado.horario_notificacao_padrao == time(19, 0)
