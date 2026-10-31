"""
AutoCare V2 - MaintenancePayload (C1)
Fonte única de verdade do rascunho de manutenção.

Antes, o dict do rascunho era montado "à mão" em `_upsert_maintenance_draft`, com chaves
duplicadas por alias (`km`/`odometer_km`, `data`/`date_str`/`date`, ...). Qualquer divergência
entre produtor (conversation) e consumidor (`webhook._persist_confirmed_data`) virava bug
silencioso (B1/B2/B12). Agora o produtor monta um `MaintenancePayload` e serializa com
`to_committed_data()`; o consumidor continua lendo o mesmo formato (compatível).
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class MaintenancePayload:
    itens: List[str] = field(default_factory=list)
    description: str = "Manutenção realizada"
    km: Optional[int] = None
    data: Optional[str] = None
    oficina: Optional[str] = None
    valor: Optional[float] = None
    target_vehicle_ref: Optional[str] = None
    discrepancy_warning: Optional[str] = None
    # Chaves extras preservadas do rascunho anterior (ex.: _action_state, flags internas)
    extras: Dict[str, Any] = field(default_factory=dict)

    def to_committed_data(self) -> Dict[str, Any]:
        """Serializa no formato consumido por `_persist_confirmed_data` (seção 6)."""
        payload: Dict[str, Any] = {
            **self.extras,
            "itens": list(self.itens),
            "components": list(self.itens),
            "description": self.description,
            "km": self.km,
            "odometer_km": self.km,
            "data": self.data,
            "date_str": self.data,
            "date": self.data,
            "oficina": self.oficina,
            "workshop_name": self.oficina,
            "valor": self.valor,
            "total_cost": self.valor if self.valor is not None else 0.0,
            "_target_vehicle_ref": self.target_vehicle_ref,
        }
        if self.discrepancy_warning:
            payload["_discrepancy_warning"] = self.discrepancy_warning
        return payload
