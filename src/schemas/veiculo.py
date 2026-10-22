"""
AutoCare V2 - Schemas Pydantic v2: Veículo
Validação estrita de placas brasileiras (Padrão Antigo e Mercosul).
"""

import re
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator

# Expressão Regular para validação de placas brasileiras (Antiga: ABC1234 / Mercosul: ABC1D23)
PLACA_REGEX = re.compile(r"^(?:[A-Z]{3}[0-9]{4}|[A-Z]{3}[0-9][A-Z][0-9]{2})$")


class VeiculoBase(BaseModel):
    placa: str = Field(..., description="Placa do veículo no padrão antigo (ABC-1234) ou Mercosul (ABC1D23)")
    marca: str = Field(..., min_length=1, max_length=50, description="Montadora/Fabricante")
    modelo: str = Field(..., min_length=1, max_length=50, description="Modelo do veículo")
    ano: int = Field(..., ge=1900, le=2100, description="Ano de fabricação/modelo")
    quilometragem: int = Field(default=0, ge=0, description="Quilometragem atual no odômetro")
    proprietario_id: int = Field(..., description="ID do proprietário associado")

    @field_validator("placa")
    @classmethod
    def validate_and_clean_placa(cls, value: str) -> str:
        """
        Limpa a string (remove hífens e espaços em branco), converte para maiúsculas
        e valida rigorosamente contra o padrão antigo e Mercosul via Regex.
        """
        if not value or not isinstance(value, str):
            raise ValueError("A placa deve ser uma string não vazia.")

        # 1. Sanitização: remove hífens, traços e espaços, e passa para caixa alta
        cleaned_placa = re.sub(r"[\s\-]", "", value).upper()

        # 2. Validação estrita por Expressão Regular
        if not PLACA_REGEX.match(cleaned_placa):
            raise ValueError(
                f"Placa '{value}' inválida. Deve seguir o padrão antigo (ex: ABC-1234) ou Mercosul (ex: ABC1D23)."
            )

        return cleaned_placa


class VeiculoCreate(VeiculoBase):
    pass


class VeiculoResponse(VeiculoBase):
    id: int
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
