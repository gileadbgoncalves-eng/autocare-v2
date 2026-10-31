"""
Test Suite: Auditoria de Cenários Adversariais, Chained Intents e Casos de Borda
(tests/test_audit_stress_scenarios.py)

B17 (M10): Os antigos testes que dependiam dos 5 blocos de mock hardcoded em conversation.py
foram migrados para tests/integration/test_edge_cases.py.
Este arquivo reexporta os testes reais de integração para compatibilidade com o runner de testes.
"""

from tests.integration.test_edge_cases import (
    corolla_vehicle,
    test_edge_case_1_placa_antiga_e_duplo_veiculo as test_scenario_1_cadastro_caotico_e_consulta_global,
    test_edge_case_2_oficina_estrita_e_especificacoes as test_scenario_2_sobrecarga_especificacoes_e_resolucao_temporal,
    test_edge_case_3_alerta_km_elevado as test_scenario_3_quebra_de_contexto_e_gatilho_de_alerta,
    test_edge_case_4_hodometro_invalido_e_fracao_temporal as test_scenario_4_validacao_de_borda_hodometro_negativo_e_fracao_temporal,
    test_edge_case_5_datas_impossiveis_e_recuperacao as test_scenario_5_colapso_absurdo_recuperacao_e_relatorio,
)

__all__ = [
    "corolla_vehicle",
    "test_scenario_1_cadastro_caotico_e_consulta_global",
    "test_scenario_2_sobrecarga_especificacoes_e_resolucao_temporal",
    "test_scenario_3_quebra_de_contexto_e_gatilho_de_alerta",
    "test_scenario_4_validacao_de_borda_hodometro_negativo_e_fracao_temporal",
    "test_scenario_5_colapso_absurdo_recuperacao_e_relatorio",
]
