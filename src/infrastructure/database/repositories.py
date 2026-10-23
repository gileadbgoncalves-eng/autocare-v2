"""
AutoCare V2 - Repository Layer (SQLAlchemy 2.0 Async)
Isola completamente as operações de persistência e consultas da lógica de negócios,
fazendo a ponte bidirecional entre os modelos ORM e as entidades puras Pydantic.
"""

from typing import List, Optional
from datetime import datetime
from sqlalchemy import select, delete, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.entities import (
    User,
    UserPreferences,
    Vehicle,
    MaintenanceRecord,
    Reminder,
    TrafficFine,
)
from src.domain.enums import (
    VehicleType,
    FuelType,
    UsageProfile,
    ReminderRecurrence,
    ReminderStatus,
    FineStatus,
)
from .models import (
    UserModel,
    VehicleModel,
    MaintenanceRecordModel,
    ReminderModel,
    TrafficFineModel,
)


class UserRepository:
    """Repositório de persistência para condutores/usuários."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_entity(model: UserModel) -> User:
        """Converte UserModel ORM para entidade de domínio User (Pydantic)."""
        prefs = UserPreferences(
            notify_financial=model.notify_financial,
            notify_maintenance=model.notify_maintenance,
            notify_minor_checks=model.notify_minor_checks,
            quiet_mode=model.quiet_mode,
        )
        return User(
            id=model.id,
            name=model.name,
            preferences=prefs,
            created_at=model.created_at,
        )

    async def get_by_id(self, user_id: str) -> Optional[User]:
        """Recupera usuário pelo identificador de telefone sanitizado."""
        stmt = select(UserModel).where(UserModel.id == user_id)
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def save(self, user: User) -> User:
        """Cria ou atualiza os dados cadastrais e preferências do motorista."""
        stmt = select(UserModel).where(UserModel.id == user.id)
        result = await self.session.execute(stmt)
        existing = result.scalar_one_or_none()

        if existing:
            existing.name = user.name
            existing.notify_financial = user.preferences.notify_financial
            existing.notify_maintenance = user.preferences.notify_maintenance
            existing.notify_minor_checks = user.preferences.notify_minor_checks
            existing.quiet_mode = user.preferences.quiet_mode
        else:
            new_user = UserModel(
                id=user.id,
                name=user.name,
                notify_financial=user.preferences.notify_financial,
                notify_maintenance=user.preferences.notify_maintenance,
                notify_minor_checks=user.preferences.notify_minor_checks,
                quiet_mode=user.preferences.quiet_mode,
                created_at=user.created_at,
            )
            self.session.add(new_user)

        await self.session.commit()
        return user

    async def get_or_create(self, user_id: str, name: Optional[str] = None) -> User:
        """Garante a existência do registro do usuário no banco."""
        user = await self.get_by_id(user_id)
        if not user:
            user = User(id=user_id, name=name)
            await self.save(user)
        return user

    async def reset_user_data(self, user_id: str) -> dict:
        """
        Zera completamente todos os dados do usuário no banco:
        remove todos os veículos, histórico de manutenção, multas e lembretes vinculados.
        """
        from .models import ReminderModel, TrafficFineModel

        # 1. Remove lembretes do usuário
        stmt_rem = delete(ReminderModel).where(ReminderModel.user_id == user_id)
        res_rem = await self.session.execute(stmt_rem)

        # 2. Remove manutenções do usuário
        stmt_maint = delete(MaintenanceRecordModel).where(MaintenanceRecordModel.user_id == user_id)
        res_maint = await self.session.execute(stmt_maint)

        # 3. Remove veículos (cascata limpa relacionamentos restantes)
        stmt_veh = delete(VehicleModel).where(VehicleModel.user_id == user_id)
        res_veh = await self.session.execute(stmt_veh)

        await self.session.commit()
        return {
            "vehicles_deleted": res_veh.rowcount,
            "maintenances_deleted": res_maint.rowcount,
            "reminders_deleted": res_rem.rowcount,
        }


class VehicleRepository:
    """Repositório de persistência para os veículos da garagem."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_entity(model: VehicleModel) -> Vehicle:
        """Converte VehicleModel ORM para entidade de domínio Vehicle (Pydantic)."""
        return Vehicle(
            id=model.id,
            user_id=model.user_id,
            make=model.make,
            model=model.model,
            year=model.year,
            plate=model.plate,
            engine=model.engine,
            version=model.version,
            transmission=model.transmission,
            steering=model.steering,
            vehicle_type=VehicleType(model.vehicle_type) if model.vehicle_type else VehicleType.CARRO,
            fuel_type=FuelType(model.fuel_type) if model.fuel_type else FuelType.FLEX,
            usage_profile=UsageProfile(model.usage_profile) if model.usage_profile else UsageProfile.CONVENCIONAL,
            current_km=model.current_km,
            created_at=model.created_at,
        )

    async def get_by_id(self, vehicle_id: int) -> Optional[Vehicle]:
        """Busca veículo pela chave primária."""
        stmt = select(VehicleModel).where(VehicleModel.id == vehicle_id)
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def get_vehicles_by_user_id(self, user_id: str) -> List[Vehicle]:
        """Retorna todos os veículos ativos da garagem do usuário."""
        stmt = select(VehicleModel).where(VehicleModel.user_id == user_id).order_by(VehicleModel.id)
        result = await self.session.execute(stmt)
        models = result.scalars().all()
        return [self._to_entity(m) for m in models]

    async def get_by_user_id(self, user_id: str) -> List[Vehicle]:
        """Alias para get_vehicles_by_user_id."""
        return await self.get_vehicles_by_user_id(user_id)

    async def get_by_plate(self, user_id: str, plate: str) -> Optional[Vehicle]:
        """Busca veículo pela placa associada ao usuário."""
        clean_plate = plate.upper().replace("-", "").strip()
        stmt = select(VehicleModel).where(
            VehicleModel.user_id == user_id,
            VehicleModel.plate == clean_plate
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def search_by_model_fuzzy(
        self, user_id: str, query: str, threshold: float = 0.7
    ) -> List[Vehicle]:
        """Busca aproximada de veículos por modelo/marca tolerando erros de digitação (ex: sandeiro -> Sandero)."""
        import difflib

        vehs = await self.get_vehicles_by_user_id(user_id)
        query_clean = query.strip().lower()
        query_words = query_clean.split()
        matched: List[tuple[Vehicle, float]] = []

        for v in vehs:
            model_lower = (v.model or "").lower()
            make_lower = (v.make or "").lower()
            plate_lower = (v.plate or "").lower().replace("-", "")

            if query_clean in model_lower or query_clean in make_lower or query_clean == plate_lower:
                matched.append((v, 1.0))
                continue

            highest_score = 0.0
            targets = [model_lower, make_lower] + model_lower.split() + make_lower.split()
            for word in query_words:
                if len(word) < 3:
                    continue
                for target in targets:
                    if len(target) < 3:
                        continue
                    ratio = difflib.SequenceMatcher(None, word, target).ratio()
                    if ratio > highest_score:
                        highest_score = ratio

            if highest_score >= threshold:
                matched.append((v, highest_score))

        matched.sort(key=lambda x: x[1], reverse=True)
        return [item[0] for item in matched]

    async def save(self, vehicle: Vehicle) -> Vehicle:
        """Insere ou atualiza os dados cadastrais do veículo."""
        if vehicle.id:
            stmt = select(VehicleModel).where(VehicleModel.id == vehicle.id)
            result = await self.session.execute(stmt)
            model = result.scalar_one_or_none()
            if model:
                model.make = vehicle.make
                model.model = vehicle.model
                model.year = vehicle.year
                model.plate = vehicle.plate
                model.engine = vehicle.engine
                model.version = vehicle.version
                model.transmission = vehicle.transmission
                model.steering = vehicle.steering
                model.vehicle_type = vehicle.vehicle_type.value
                model.fuel_type = vehicle.fuel_type.value
                model.usage_profile = vehicle.usage_profile.value
                model.current_km = vehicle.current_km
                await self.session.commit()
                return self._to_entity(model)

        new_model = VehicleModel(
            user_id=vehicle.user_id,
            make=vehicle.make,
            model=vehicle.model,
            year=vehicle.year,
            plate=vehicle.plate,
            engine=vehicle.engine,
            version=vehicle.version,
            transmission=vehicle.transmission,
            steering=vehicle.steering,
            vehicle_type=vehicle.vehicle_type.value,
            fuel_type=vehicle.fuel_type.value,
            usage_profile=vehicle.usage_profile.value,
            current_km=vehicle.current_km,
            created_at=vehicle.created_at,
        )
        self.session.add(new_model)
        await self.session.commit()
        await self.session.refresh(new_model)
        return self._to_entity(new_model)

    async def add_vehicle(self, vehicle: Vehicle) -> Vehicle:
        """Alias para save (inserção/persistência de veículo)."""
        return await self.save(vehicle)

    async def update_km(self, vehicle_id: int, km: int) -> Optional[Vehicle]:
        """Atualiza a quilometragem atual do veículo de forma atômica."""
        stmt = select(VehicleModel).where(VehicleModel.id == vehicle_id)
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        if not model:
            return None

        # Só atualiza se o novo KM for maior ou igual (proteção contra regressão de odômetro)
        if model.current_km is None or km >= model.current_km:
            model.current_km = km
            await self.session.commit()
            await self.session.refresh(model)

        return self._to_entity(model)

    async def delete_all_by_user_id(self, user_id: str) -> int:
        """Remove todos os veículos da garagem do condutor (Reset de Garagem)."""
        stmt = delete(VehicleModel).where(VehicleModel.user_id == user_id)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount


class MaintenanceRepository:
    """Repositório de persistência para registros do Porta-Luvas Digital."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_entity(model: MaintenanceRecordModel) -> MaintenanceRecord:
        """Converte MaintenanceRecordModel ORM para entidade Pydantic."""
        return MaintenanceRecord(
            id=model.id,
            vehicle_id=model.vehicle_id,
            user_id=model.user_id,
            components=model.components or [],
            description=model.description,
            km=model.km,
            date=model.date,
            total_cost=model.total_cost,
            parts_cost=model.parts_cost,
            labor_cost=model.labor_cost,
            workshop_name=model.workshop_name,
            parts_brand=model.parts_brand,
            warranty_months=model.warranty_months,
            invoice_photo_url=model.invoice_photo_url,
            created_at=model.created_at,
        )

    async def get_by_id(self, record_id: int) -> Optional[MaintenanceRecord]:
        """Busca registro de manutenção pela chave primária."""
        stmt = select(MaintenanceRecordModel).where(MaintenanceRecordModel.id == record_id)
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def add_maintenance_record(self, record: MaintenanceRecord) -> MaintenanceRecord:
        """Persiste um novo registro de manutenção e atualiza o KM do veículo se aplicável."""
        new_model = MaintenanceRecordModel(
            vehicle_id=record.vehicle_id,
            user_id=record.user_id,
            components=record.components,
            description=record.description,
            km=record.km,
            date=record.date,
            total_cost=record.total_cost,
            parts_cost=record.parts_cost,
            labor_cost=record.labor_cost,
            workshop_name=record.workshop_name,
            parts_brand=record.parts_brand,
            warranty_months=record.warranty_months,
            invoice_photo_url=record.invoice_photo_url,
            created_at=record.created_at,
        )
        self.session.add(new_model)

        # Atualiza o KM do veículo caso a quilometragem da revisão seja mais recente
        if record.km:
            stmt = select(VehicleModel).where(VehicleModel.id == record.vehicle_id)
            res = await self.session.execute(stmt)
            veh = res.scalar_one_or_none()
            if veh and (veh.current_km is None or record.km > veh.current_km):
                veh.current_km = record.km

        await self.session.commit()
        await self.session.refresh(new_model)
        return self._to_entity(new_model)

    async def add_record(self, record: MaintenanceRecord) -> MaintenanceRecord:
        """Alias para add_maintenance_record."""
        return await self.add_maintenance_record(record)

    async def get_by_vehicle_id(self, vehicle_id: int, limit: int = 50) -> List[MaintenanceRecord]:
        """Retorna histórico cronológico decrescente de manutenções de um veículo."""
        stmt = (
            select(MaintenanceRecordModel)
            .where(MaintenanceRecordModel.vehicle_id == vehicle_id)
            .order_by(desc(MaintenanceRecordModel.date), desc(MaintenanceRecordModel.id))
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        models = result.scalars().all()
        return [self._to_entity(m) for m in models]

    async def get_latest_by_vehicle_id(self, vehicle_id: int) -> Optional[MaintenanceRecord]:
        """Obtém a manutenção mais recente registrada para o veículo."""
        stmt = (
            select(MaintenanceRecordModel)
            .where(MaintenanceRecordModel.vehicle_id == vehicle_id)
            .order_by(desc(MaintenanceRecordModel.date), desc(MaintenanceRecordModel.id))
            .limit(1)
        )
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def get_total_cost_by_vehicle_id(self, vehicle_id: int) -> float:
        """Calcula o valor total acumulado investido em manutenções do veículo."""
        stmt = select(func.sum(MaintenanceRecordModel.total_cost)).where(
            MaintenanceRecordModel.vehicle_id == vehicle_id
        )
        result = await self.session.execute(stmt)
        total = result.scalar()
        return float(total) if total is not None else 0.0

    async def get_by_user_id(self, user_id: str, limit: int = 50) -> List[MaintenanceRecord]:
        """Retorna histórico cronológico de todas as manutenções dos veículos do usuário."""
        stmt = (
            select(MaintenanceRecordModel)
            .where(MaintenanceRecordModel.user_id == user_id)
            .order_by(desc(MaintenanceRecordModel.date), desc(MaintenanceRecordModel.id))
            .limit(limit)
        )
        result = await self.session.execute(stmt)
        models = result.scalars().all()
        return [self._to_entity(m) for m in models]

    async def delete_all_by_user_id(self, user_id: str) -> int:
        """Remove todos os registros de manutenção do usuário."""
        stmt = delete(MaintenanceRecordModel).where(MaintenanceRecordModel.user_id == user_id)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount



class ReminderRepository:
    """Repositório de persistência para lembretes automotivos."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_entity(model: ReminderModel) -> Reminder:
        return Reminder(
            id=model.id,
            user_id=model.user_id,
            vehicle_id=model.vehicle_id,
            description=model.description,
            due_datetime=model.due_datetime,
            data_hora_agendada=model.data_hora_agendada,
            due_km=model.due_km,
            recurrence=ReminderRecurrence(model.recurrence) if model.recurrence in [e.value for e in ReminderRecurrence] else ReminderRecurrence.ONCE,
            status=ReminderStatus(model.status) if model.status in [e.value for e in ReminderStatus] else ReminderStatus.PENDING,
            created_at=model.created_at,
        )

    async def add_reminder(self, reminder: Reminder) -> Reminder:
        new_model = ReminderModel(
            user_id=reminder.user_id,
            vehicle_id=reminder.vehicle_id,
            description=reminder.description,
            due_datetime=reminder.due_datetime,
            data_hora_agendada=reminder.data_hora_agendada,
            due_km=reminder.due_km,
            recurrence=reminder.recurrence.value if hasattr(reminder.recurrence, "value") else str(reminder.recurrence),
            status=reminder.status.value if hasattr(reminder.status, "value") else str(reminder.status),
            created_at=reminder.created_at,
        )
        self.session.add(new_model)
        await self.session.commit()
        await self.session.refresh(new_model)
        return self._to_entity(new_model)

    async def get_by_user_id(self, user_id: str) -> List[Reminder]:
        stmt = (
            select(ReminderModel)
            .where(ReminderModel.user_id == user_id)
            .order_by(desc(ReminderModel.created_at))
        )
        result = await self.session.execute(stmt)
        models = result.scalars().all()
        return [self._to_entity(m) for m in models]

    async def get_by_id(self, reminder_id: int) -> Optional[Reminder]:
        stmt = select(ReminderModel).where(ReminderModel.id == reminder_id)
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def delete(self, reminder_id: int) -> bool:
        stmt = delete(ReminderModel).where(ReminderModel.id == reminder_id)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0


class TrafficFineRepository:
    """Repositório de persistência para multas e infrações de trânsito."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def _to_entity(model: TrafficFineModel) -> TrafficFine:
        return TrafficFine(
            id=model.id,
            vehicle_id=model.vehicle_id,
            description=model.description,
            infraction_code=model.infraction_code,
            issuing_body=model.issuing_body,
            points=model.points,
            amount=model.amount,
            discounted_amount=model.discounted_amount,
            infraction_date=model.infraction_date,
            due_date=model.due_date,
            status=FineStatus(model.status) if model.status in [e.value for e in FineStatus] else FineStatus.NOTIFICADA,
            created_at=model.created_at,
        )

    async def add_fine(self, fine: TrafficFine) -> TrafficFine:
        new_model = TrafficFineModel(
            vehicle_id=fine.vehicle_id,
            description=fine.description,
            infraction_code=fine.infraction_code,
            issuing_body=fine.issuing_body,
            points=fine.points,
            amount=fine.amount,
            discounted_amount=fine.discounted_amount,
            infraction_date=fine.infraction_date,
            due_date=fine.due_date,
            status=fine.status.value if hasattr(fine.status, "value") else str(fine.status),
            created_at=fine.created_at,
        )
        self.session.add(new_model)
        await self.session.commit()
        await self.session.refresh(new_model)
        return self._to_entity(new_model)

    async def get_by_user_id(self, user_id: str) -> List[TrafficFine]:
        stmt = (
            select(TrafficFineModel)
            .join(VehicleModel, TrafficFineModel.vehicle_id == VehicleModel.id)
            .where(VehicleModel.user_id == user_id)
            .order_by(desc(TrafficFineModel.created_at))
        )
        result = await self.session.execute(stmt)
        models = result.scalars().all()
        return [self._to_entity(m) for m in models]

    async def get_by_id(self, fine_id: int) -> Optional[TrafficFine]:
        stmt = select(TrafficFineModel).where(TrafficFineModel.id == fine_id)
        result = await self.session.execute(stmt)
        model = result.scalar_one_or_none()
        return self._to_entity(model) if model else None

    async def delete(self, fine_id: int) -> bool:
        stmt = delete(TrafficFineModel).where(TrafficFineModel.id == fine_id)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount > 0
