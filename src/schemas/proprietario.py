"""
AutoCare V2 - Schemas Pydantic v2: Proprietário
"""

from datetime import datetime, time
from typing import Optional, List
from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ProprietarioBase(BaseModel):
    nome: str = Field(..., min_length=2, max_length=100, description="Nome completo do proprietário")
    email: EmailStr = Field(..., description="E-mail único de contato")
    telefone: str = Field(..., min_length=8, max_length=20, description="Telefone de contato com DDD")
    horario_notificacao_padrao: time = Field(
        default=time(9, 0), description="Horário preferido para receber o Check-up Diário da garagem (padrão 09:00)"
    )


class ProprietarioCreate(ProprietarioBase):
    pass


class ProprietarioUpdate(BaseModel):
    nome: Optional[str] = Field(default=None, min_length=2, max_length=100)
    email: Optional[EmailStr] = None
    telefone: Optional[str] = Field(default=None, min_length=8, max_length=20)
    horario_notificacao_padrao: Optional[time] = Field(
        default=None, description="Atualizar horário preferencial do check-up diário"
    )


class ProprietarioResponse(ProprietarioBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)

