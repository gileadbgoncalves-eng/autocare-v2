"""
AutoCare V2 - SQLAlchemy 2.0 ORM Models
Mapeamento declarativo relacional espelhando as entidades de domínio Pydantic.
"""

from datetime import datetime, date
from typing import Optional, List
from sqlalchemy import (
    String,
    Integer,
    Float,
    Boolean,
    DateTime,
    Date,
    Text,
    ForeignKey,
    JSON,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .session import Base
from src.domain.enums import (
    VehicleType,
    FuelType,
    UsageProfile,
    ReminderRecurrence,
    ReminderStatus,
    FineStatus,
)


class UserModel(Base):
    """Tabela de motoristas identificados pelo telefone E.164."""
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    notify_financial: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_maintenance: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_minor_checks: Mapped[bool] = mapped_column(Boolean, default=True)
    quiet_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relacionamentos
    vehicles: Mapped[List["VehicleModel"]] = relationship(
        "VehicleModel",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    reminders: Mapped[List["ReminderModel"]] = relationship(
        "ReminderModel",
        back_populates="user",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class VehicleModel(Base):
    """Tabela de veículos associados à garagem do condutor."""
    __tablename__ = "vehicles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    make: Mapped[str] = mapped_column(String(60), nullable=False)
    model: Mapped[str] = mapped_column(String(80), nullable=False)
    year: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    plate: Mapped[Optional[str]] = mapped_column(String(15), nullable=True, index=True)
    engine: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    version: Mapped[Optional[str]] = mapped_column(String(60), nullable=True)
    transmission: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    steering: Mapped[Optional[str]] = mapped_column(String(40), nullable=True)
    vehicle_type: Mapped[str] = mapped_column(String(30), default=VehicleType.CARRO.value)
    fuel_type: Mapped[str] = mapped_column(String(30), default=FuelType.FLEX.value)
    usage_profile: Mapped[str] = mapped_column(String(30), default=UsageProfile.CONVENCIONAL.value)
    current_km: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relacionamentos
    user: Mapped["UserModel"] = relationship("UserModel", back_populates="vehicles")
    maintenances: Mapped[List["MaintenanceRecordModel"]] = relationship(
        "MaintenanceRecordModel",
        back_populates="vehicle",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="desc(MaintenanceRecordModel.date)",
    )
    reminders: Mapped[List["ReminderModel"]] = relationship(
        "ReminderModel",
        back_populates="vehicle",
        cascade="all, delete-orphan",
        lazy="selectin",
    )
    fines: Mapped[List["TrafficFineModel"]] = relationship(
        "TrafficFineModel",
        back_populates="vehicle",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class MaintenanceRecordModel(Base):
    """Tabela de histórico do Porta-Luvas Digital (revisões e manutenções)."""
    __tablename__ = "maintenance_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    components: Mapped[List[str]] = mapped_column(JSON, nullable=False, default=list)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    km: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, default=date.today)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    parts_cost: Mapped[float] = mapped_column(Float, default=0.0)
    labor_cost: Mapped[float] = mapped_column(Float, default=0.0)
    workshop_name: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    parts_brand: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    warranty_months: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    invoice_photo_url: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relacionamentos
    vehicle: Mapped["VehicleModel"] = relationship("VehicleModel", back_populates="maintenances")


class ReminderModel(Base):
    """Tabela de lembretes ativos e proativos para disparo no WhatsApp."""
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(
        String(30), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    vehicle_id: Mapped[Optional[int]] = mapped_column(
        Integer, ForeignKey("vehicles.id", ondelete="SET NULL"), nullable=True, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    due_datetime: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    due_km: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    recurrence: Mapped[str] = mapped_column(String(30), default=ReminderRecurrence.ONCE.value)
    status: Mapped[str] = mapped_column(String(30), default=ReminderStatus.PENDING.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relacionamentos
    user: Mapped["UserModel"] = relationship("UserModel", back_populates="reminders")
    vehicle: Mapped[Optional["VehicleModel"]] = relationship("VehicleModel", back_populates="reminders")


class TrafficFineModel(Base):
    """Tabela de multas e infrações vinculadas a veículos."""
    __tablename__ = "traffic_fines"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    vehicle_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("vehicles.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    infraction_code: Mapped[Optional[str]] = mapped_column(String(30), nullable=True)
    issuing_body: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[float] = mapped_column(Float, default=0.0)
    discounted_amount: Mapped[float] = mapped_column(Float, default=0.0)
    infraction_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    due_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default=FineStatus.NOTIFICADA.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    # Relacionamentos
    vehicle: Mapped["VehicleModel"] = relationship("VehicleModel", back_populates="fines")
