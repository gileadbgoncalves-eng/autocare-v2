"""
AutoCare V2 - Schemas Pydantic v2: Proprietário
"""

from datetime import datetime
from typing import Optional, List
from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ProprietarioBase(BaseModel):
    nome: str = Field(..., min_length=2, max_length=100, description="Nome completo do proprietário")
    email: EmailStr = Field(..., description="E-mail único de contato")
    telefone: str = Field(..., min_length=8, max_length=20, description="Telefone de contato com DDD")


class ProprietarioCreate(ProprietarioBase):
    pass


class ProprietarioResponse(ProprietarioBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
