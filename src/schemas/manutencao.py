"""
AutoCare V2 - Schemas Pydantic v2: Manutenção
"""

from datetime import datetime, date
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class ManutencaoBase(BaseModel):
    veiculo_id: int = Field(..., description="ID do veículo atendido")
    descricao: str = Field(..., min_length=2, max_length=255, description="Descrição do serviço ou peças")
    custo: float = Field(default=0.0, ge=0.0, description="Valor total da manutenção")
    data: date = Field(default_factory=date.today, description="Data da realização do serviço")
    odometro_registro: int = Field(..., ge=0, description="Quilometragem registrada no momento da manutenção")


class ManutencaoCreate(ManutencaoBase):
    pass


class ManutencaoResponse(ManutencaoBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
