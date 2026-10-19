"""
AutoCare V2 - Repository Layer (SQLAlchemy 2.0 Async)
Isola completamente as operações de persistência e consultas da lógica de negócios,
fazendo a ponte bidirecional entre os modelos ORM e as entidades puras Pydantic.
"""

from typing import List, Optional
from datetime import datetime
from sqlalchemy import select, delete, desc, func
from sqlalchemy.ext.asyncio import AsyncSession

from src.domain.entities import User, UserPreferences, Vehicle, MaintenanceRecord
from src.domain.enums import VehicleType, FuelType, UsageProfile
from .models import UserModel, VehicleModel, MaintenanceRecordModel


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

    async def delete_all_by_user_id(self, user_id: str) -> int:
        """Remove todos os registros de manutenção do usuário."""
        stmt = delete(MaintenanceRecordModel).where(MaintenanceRecordModel.user_id == user_id)
        result = await self.session.execute(stmt)
        await self.session.commit()
        return result.rowcount
