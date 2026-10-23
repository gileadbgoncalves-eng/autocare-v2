"""
AutoCare V2 - Schemas Pydantic v2: Lembrete
"""

from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class LembreteBase(BaseModel):
    veiculo_id: int = Field(..., description="ID do veículo associado")
    descricao: str = Field(..., min_length=2, max_length=255, description="Descrição da tarefa ou checagem preventiva")
    data_hora_agendada: Optional[datetime] = Field(default=None, description="Data e hora exatas agendadas para o disparo")
    odometro_alvo: Optional[int] = Field(default=None, ge=0, description="Hodômetro alvo para o disparo")
    status: str = Field(default="pendente", description="Status do lembrete: pendente, agendado, concluido")


class LembreteCreate(LembreteBase):
    pass


class LembreteUpdate(BaseModel):
    descricao: Optional[str] = Field(default=None, min_length=2, max_length=255)
    data_hora_agendada: Optional[datetime] = None
    odometro_alvo: Optional[int] = Field(default=None, ge=0)
    status: Optional[str] = None


class LembreteResponse(LembreteBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
