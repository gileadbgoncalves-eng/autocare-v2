"""
AutoCare V2 - Notification & Reminder Service
Motor assíncrono de notificações, consolidação em lote (batching) e agendamento proativo.

Funcionalidades:
1. Agrupamento em lote (Batching): Consolida todos os alertas dos veículos do condutor
   em um único boletim diário elegante, eliminando mensagens fragmentadas (spam).
2. Isolamento estrito por Foreign Key (FK): Garante que alertas de um veículo (ex: Civic)
   nunca vazem para outro (ex: Sandero).
3. Proteção contra disparo antecipado: Lembretes agendados para horários específicos
   futuros (ex: 08:00) são estritamente ignorados pelo boletim padrão das 05:00.
4. Lembretes pontuais isolados: Disparados no minuto exato agendado e marcados como 'concluido'.
5. Horário customizável: O boletim diário é enviado apenas no horário_notificacao_padrao do condutor.
"""

import asyncio
import logging
from datetime import datetime, date, time
from typing import Dict, Any, List, Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession

from src.config.settings import settings
from src.database import async_session_factory
from src.models.proprietario import Proprietario
from src.models.veiculo import Veiculo
from src.models.lembrete import Lembrete
from src.repositories.proprietario_repository import ProprietarioRepository
from src.repositories.veiculo_repository import VeiculoRepository
from src.repositories.lembrete_repository import LembreteRepository

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False

logger = logging.getLogger("autocare.notifications")


class NotificationService:
    """
    Serviço central de agregação e despacho de notificações para WhatsApp.
    """

    def __init__(self, session_factory=None):
        self.session_factory = session_factory or async_session_factory
        # Histórico de envios em memória para auditoria e asserção em testes
        self.outbound_history: List[Dict[str, Any]] = []

    async def send_whatsapp_message(self, telefone: str, message: str) -> bool:
        """
        Dispara mensagem para o condutor via gateway configurado (Evolution API / Z-API).
        Registra no histórico para rastreabilidade e testes.
        """
        clean_phone = "".join(filter(str.isdigit, str(telefone)))
        record = {
            "telefone": clean_phone,
            "message": message,
            "timestamp": datetime.now(),
        }
        self.outbound_history.append(record)
        logger.info(f"📤 [WhatsApp Outbound] Para: {clean_phone} | Mensagem:\n{message}")

        if not HAS_HTTPX or not message:
            return True

        try:
            if settings.PROVIDER == "evolution" and settings.EVOLUTION_BASE_URL and settings.EVOLUTION_API_KEY:
                url = f"{settings.EVOLUTION_BASE_URL.rstrip('/')}/message/sendText/{settings.EVOLUTION_INSTANCE}"
                headers = {
                    "apikey": settings.EVOLUTION_API_KEY,
                    "Content-Type": "application/json",
                }
                body = {
                    "number": clean_phone,
                    "text": message,
                    "delay": settings.EVOLUTION_TYPING_DELAY_MS,
                }
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(url, json=body, headers=headers)
                return True

            elif settings.PROVIDER == "zapi" and settings.ZAPI_INSTANCE_ID and settings.ZAPI_TOKEN:
                url = f"https://api.z-api.io/instances/{settings.ZAPI_INSTANCE_ID}/token/{settings.ZAPI_TOKEN}/send-text"
                headers = {"Content-Type": "application/json"}
                if settings.ZAPI_CLIENT_TOKEN:
                    headers["Client-Token"] = settings.ZAPI_CLIENT_TOKEN
                body = {
                    "phone": clean_phone,
                    "message": message,
                }
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.post(url, json=body, headers=headers)
                return True

        except Exception as e:
            logger.warning(f"Gateway WhatsApp outbound offline/ignorado: {e}")

        return True

    @staticmethod
    def _get_vehicle_icon(modelo: str, index: int = 0) -> str:
        """Retorna emoji elegante dependendo do tipo do veículo."""
        mod_lower = modelo.lower()
        if any(m in mod_lower for m in ["cg", "titan", "fazer", "moto", "twister", "biz", "scooter"]):
            return "🏍️"
        car_icons = ["🚗", "🚙", "🛻", "🏎️"]
        return car_icons[index % len(car_icons)]

    async def build_garage_bulletin(
        self,
        session: AsyncSession,
        proprietario_id: int,
        current_dt: Optional[datetime] = None,
    ) -> Optional[str]:
        """
        PASSO 2: Motor de Agregação (Batching)
        Consolida alertas de todos os veículos de um proprietário em uma única mensagem.
        
        Regras Críticas:
        1. Isolamento por FK: Um alerta só é associado ao veículo se alert.veiculo_id == veiculo.id.
        2. Bypass de Agendamento Futuro: Se um item possui data_hora_agendada futura,
           ele é estritamente ignorado deste boletim geral (para evitar disparo duplo).
        3. Se nenhum veículo possuir pendências, retorna None (sem spam).
        """
        now = current_dt or datetime.now()
        prop_repo = ProprietarioRepository(session)
        veic_repo = VeiculoRepository(session)
        lemb_repo = LembreteRepository(session)

        proprietario = await prop_repo.get_by_id(proprietario_id)
        if not proprietario:
            return None

        veiculos = await veic_repo.get_by_proprietario_id(proprietario_id)
        if not veiculos:
            return None

        veiculos_com_alertas: List[Tuple[Veiculo, List[str]]] = []

        for v in veiculos:
            # Busca lembretes gerais elegíveis (exclui agendados para horários futuros)
            lembretes = await lemb_repo.get_general_pending_for_vehicle(v.id, current_dt=now)

            # Filtro adicional estrito de integridade FK e agendamento futuro
            alertas_veiculo: List[str] = []
            for lem in lembretes:
                # Garantia absoluta de Foreign Key
                if lem.veiculo_id != v.id:
                    continue
                # Se houver data_hora_agendada futura explícita, IGNORA no boletim global
                if lem.data_hora_agendada and lem.data_hora_agendada > now:
                    continue
                alertas_veiculo.append(lem.descricao)

            if alertas_veiculo:
                veiculos_com_alertas.append((v, alertas_veiculo))

        if not veiculos_com_alertas:
            return None

        # Montagem do resumo consolidado elegante
        primeiro_nome = proprietario.nome.split()[0] if proprietario.nome else "motorista"
        linhas = [
            f"Bom dia, {primeiro_nome}! Que tal verificar sua garagem hoje?"
        ]

        for idx, (veiculo, alertas) in enumerate(veiculos_com_alertas):
            icon = self._get_vehicle_icon(veiculo.modelo, idx)
            # Concatena alertas do veículo
            resumo_alertas = "; ".join(alertas)
            if not resumo_alertas.endswith("."):
                resumo_alertas += "."
            linhas.append(f"{icon} {veiculo.modelo}: {resumo_alertas}")

        return "\n".join(linhas)

    async def dispatch_daily_bulletins(
        self,
        session: AsyncSession,
        target_time: Optional[time] = None,
        current_dt: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        PASSO 3: Consulta o banco e só dispara o 'Boletim Diário' para usuários
        cujo horario_notificacao_padrao coincide com o horário alvo (hora e minuto).
        """
        now = current_dt or datetime.now()
        check_time = target_time or now.time()

        prop_repo = ProprietarioRepository(session)
        # Filtra proprietários com horário padrão correspondente
        candidatos = await prop_repo.get_by_horario_notificacao(check_time)
        logger.info(f"Encontrados {len(candidatos)} condutores para o Boletim Diário das {check_time.strftime('%H:%M')}")

        disparos_realizados = []
        for prop in candidatos:
            bulletin = await self.build_garage_bulletin(session, prop.id, current_dt=now)
            if bulletin:
                await self.send_whatsapp_message(prop.telefone, bulletin)
                disparos_realizados.append({
                    "proprietario_id": prop.id,
                    "telefone": prop.telefone,
                    "horario": check_time.strftime("%H:%M"),
                    "bulletin": bulletin,
                })

        return disparos_realizados

    async def dispatch_scheduled_reminders(
        self,
        session: AsyncSession,
        current_dt: Optional[datetime] = None,
    ) -> List[Dict[str, Any]]:
        """
        PASSO 3: Tarefas agendadas para horários específicos (ex: 08:00)
        rodam de forma isolada, no minuto exato solicitado pelo usuário.
        Uma vez disparadas, são marcadas como 'concluido'.
        """
        now = current_dt or datetime.now()
        lemb_repo = LembreteRepository(session)
        veic_repo = VeiculoRepository(session)
        prop_repo = ProprietarioRepository(session)

        # Lembretes que venceram até o instante 'now'
        due_reminders = await lemb_repo.get_due_scheduled_reminders(now)
        logger.info(f"Encontrados {len(due_reminders)} lembretes agendados vencidos em {now.strftime('%d/%m/%Y %H:%M')}")

        disparos_pontuais = []
        for lem in due_reminders:
            veiculo = await veic_repo.get_by_id(lem.veiculo_id)
            if not veiculo:
                continue

            proprietario = await prop_repo.get_by_id(veiculo.proprietario_id)
            if not proprietario:
                continue

            msg = f"⏰ *Lembrete agendado AutoCare:* Seu {veiculo.modelo} precisa de: {lem.descricao}."
            await self.send_whatsapp_message(proprietario.telefone, msg)

            # Marca como concluído
            await lemb_repo.update_status(lem.id, "concluido")

            disparos_pontuais.append({
                "lembrete_id": lem.id,
                "veiculo_id": veiculo.id,
                "modelo": veiculo.modelo,
                "proprietario_id": proprietario.id,
                "mensagem": msg,
            })

        return disparos_pontuais

    async def run_scheduler_tick(
        self,
        session: Optional[AsyncSession] = None,
        current_dt: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        """
        Executa um ciclo do scheduler:
        1. Lembretes pontuais agendados para o instante exato.
        2. Boletins diários consolidados para condutores no seu horário padrão.
        """
        now = current_dt or datetime.now()

        if session:
            pontuais = await self.dispatch_scheduled_reminders(session, current_dt=now)
            boletins = await self.dispatch_daily_bulletins(session, target_time=now.time(), current_dt=now)
            return {"now": now, "pontuais": pontuais, "boletins": boletins}

        async with self.session_factory() as sess:
            pontuais = await self.dispatch_scheduled_reminders(sess, current_dt=now)
            boletins = await self.dispatch_daily_bulletins(sess, target_time=now.time(), current_dt=now)
            return {"now": now, "pontuais": pontuais, "boletins": boletins}


class NotificationScheduler:
    """
    Controlador de tarefa em segundo plano para o loop periódico do Scheduler.
    Integrado ao Lifespan do FastAPI.
    """

    def __init__(self, service: Optional[NotificationService] = None, interval_seconds: int = 60):
        self.service = service or NotificationService()
        self.interval_seconds = interval_seconds
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        """Inicia o loop assíncrono em segundo plano."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(f"⏱️ NotificationScheduler iniciado com intervalo de {self.interval_seconds}s.")

    async def stop(self) -> None:
        """Encerra graciosamente o loop de agendamento."""
        if not self._running:
            return
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 NotificationScheduler encerrado com sucesso.")

    async def _loop(self) -> None:
        while self._running:
            try:
                await self.service.run_scheduler_tick()
            except Exception as e:
                logger.error(f"Erro no ciclo do NotificationScheduler: {e}", exc_info=True)
            await asyncio.sleep(self.interval_seconds)


# Instância global padrão
notification_service = NotificationService()
notification_scheduler = NotificationScheduler(service=notification_service)
