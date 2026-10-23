"""
AutoCare V2 - Interactive Local Terminal Test Script (test_bot.py)
Permite testar localmente todo o fluxo conversacional: Gemini NLP, máquina de estados,
fluxo de confirmação (Human-in-the-Loop) e persistência relacional assíncrona.
"""

import asyncio
import sys
from typing import Optional

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src.infrastructure.database.session import init_db, close_db, async_session_factory
from src.infrastructure.database.repositories import (
    UserRepository,
    VehicleRepository,
    MaintenanceRepository,
)
from src.application.services.conversation import ConversationService
from src.domain.entities import Vehicle, MaintenanceRecord
from src.domain.enums import VehicleType, FuelType, UsageProfile


async def main():
    print("=" * 70)
    print("🚗 AutoCare V2 - Terminal de Teste Interativo")
    print("=" * 70)
    print("Inicializando banco de dados assíncrono...")
    await init_db()
    print("✅ Banco inicializado com sucesso!")
    print("Digite suas mensagens como se estivesse conversando no WhatsApp.")
    print("Comandos rápidos: 'zerar sistema' (limpa tudo do zero), 'sair' (encerra o teste).\n")

    test_user_id = "5521999998888"
    conversation_service = ConversationService()

    # Garante usuário no banco
    async with async_session_factory() as session:
        user_repo = UserRepository(session)
        await user_repo.get_or_create(user_id=test_user_id, name="Motorista Teste")

    while True:
        try:
            user_input = input("Você: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nEncerrando...")
            break

        if not user_input:
            continue

        if user_input.lower() in ("sair", "exit", "quit"):
            print("\n👋 Sessão de teste finalizada. Até logo!")
            break

        async with async_session_factory() as session:
            user_repo = UserRepository(session)
            vehicle_repo = VehicleRepository(session)
            maintenance_repo = MaintenanceRepository(session)

            # 1. Carrega veículos cadastrados na garagem
            vehicles = await vehicle_repo.get_vehicles_by_user_id(user_id=test_user_id)

            # 2. Processa a mensagem através da máquina de estados e Gemini
            result = await conversation_service.handle_incoming_message(
                user_id=test_user_id,
                message_text=user_input,
                vehicles=vehicles,
            )

            action_type = result.get("action_type")
            committed_data = result.get("committed_data")
            state = result.get("state")
            reply = result.get("reply")

            # 3. Persistência real em caso de ação confirmada OU system_reset
            if action_type in ("action_confirmed", "system_reset", "action_confirmed_with_chained_reminder") and committed_data:
                try:
                    # Reset de Garagem / Sistema
                    if (
                        action_type == "system_reset"
                        or committed_data.get("_reset_system")
                        or committed_data.get("_reset_garage")
                        or "reset" in str(committed_data).lower()
                    ):
                        reset_stats = await user_repo.reset_user_data(test_user_id)
                        print(f"  🧹 [BANCO] Sistema e garagem zerados com sucesso: {reset_stats}")

                    # Cadastro de Veículo
                    elif "make" in committed_data and "model" in committed_data and "components" not in committed_data:
                        new_veh = Vehicle(
                            user_id=test_user_id,
                            make=committed_data.get("make", "Veículo"),
                            model=committed_data.get("model", "Modelo"),
                            year=committed_data.get("year"),
                            plate=committed_data.get("plate"),
                            engine=committed_data.get("engine"),
                            current_km=committed_data.get("current_km"),
                            vehicle_type=VehicleType(committed_data.get("vehicle_type", "carro")) if committed_data.get("vehicle_type") in [e.value for e in VehicleType] else VehicleType.CARRO,
                            fuel_type=FuelType(committed_data.get("fuel_type", "flex")) if committed_data.get("fuel_type") in [e.value for e in FuelType] else FuelType.FLEX,
                            usage_profile=UsageProfile(committed_data.get("usage_profile", "convencional")) if committed_data.get("usage_profile") in [e.value for e in UsageProfile] else UsageProfile.CONVENCIONAL,
                        )
                        saved = await vehicle_repo.add_vehicle(new_veh)
                        print(f"  💾 [BANCO] Veículo persistido no banco com ID: {saved.id}")

                    # Registro de Manutenção
                    elif "components" in committed_data or "labor_cost" in committed_data or "total_cost" in committed_data:
                        target_id = vehicles[0].id if vehicles else 1
                        new_maint = MaintenanceRecord(
                            vehicle_id=target_id,
                            user_id=test_user_id,
                            components=committed_data.get("components") or [committed_data.get("description", "Manutenção")],
                            description=committed_data.get("description", "Serviço realizado"),
                            km=committed_data.get("km"),
                            total_cost=float(committed_data.get("total_cost", 0.0) or 0.0),
                            parts_cost=float(committed_data.get("parts_cost", 0.0) or 0.0),
                            labor_cost=float(committed_data.get("labor_cost", 0.0) or 0.0),
                            workshop_name=committed_data.get("workshop_name"),
                            parts_brand=committed_data.get("parts_brand"),
                        )
                        saved = await maintenance_repo.add_record(new_maint)
                        print(f"  💾 [BANCO] Manutenção gravada no banco com ID: {saved.id}")

                    # Atualização Exclusiva de Odômetro
                    elif "odometer_km" in committed_data or committed_data.get("_is_odometer_update"):
                        target_id = vehicles[0].id if vehicles else 1
                        new_km = committed_data.get("odometer_km") or committed_data.get("km")
                        if new_km:
                            await vehicle_repo.update_km(target_id, int(new_km))
                            print(f"  💾 [BANCO] Odômetro atualizado no banco para {new_km} km")

                    # Lembrete Encadeado
                    if committed_data.get("chained_reminder"):
                        c_rem = committed_data["chained_reminder"]
                        target_id = vehicles[0].id if vehicles else 1
                        from src.domain.entities import Reminder
                        from src.infrastructure.database.repositories import ReminderRepository
                        rem_repo = ReminderRepository(session)
                        new_rem = Reminder(
                            user_id=test_user_id,
                            vehicle_id=target_id,
                            description=c_rem.get("task_description", "Revisão preventiva"),
                        )
                        saved_rem = await rem_repo.add_reminder(new_rem)
                        print(f"  💾 [BANCO] Lembrete encadeado gravado com ID: {saved_rem.id}")

                except Exception as ex:
                    print(f"  ⚠️ [ERRO AO PERSISTIR]: {ex}")

            # 4. Exibição da resposta e metadados de depuração
            print(f"\n🤖 AutoCare: {reply}")
            print(f"   [Debug -> Estado: {state} | Ação: {action_type}]\n")

    await close_db()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
