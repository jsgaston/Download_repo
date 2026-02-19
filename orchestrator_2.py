"""
Kraken Futures Orchestrator
============================
🔄 Bucle automático con ejecución PARALELA:
   - Bot2 (monitor) arranca en hilo de fondo al inicio del ciclo
   - Bot1 (abrir órdenes) corre en paralelo, sin bloquear el monitor
   - Si el objetivo se alcanza mientras Bot1 escanea → cierra inmediatamente
   - Al terminar Bot1, espera a que Bot2 confirme el cierre total
   - Cuando Bot2 termina → pausa → nuevo ciclo

✅ Botones Telegram: Parar Bot | Cerrar Todo | Ver Posiciones | Cerrar posición individual
"""

import time
import threading
import sys
from datetime import datetime

try:
    import telegram_notifier as tg
    _TG_AVAILABLE = True
except ImportError:
    _TG_AVAILABLE = False
    print("⚠️  telegram_notifier.py no encontrado — notificaciones Telegram desactivadas")

try:
    from FuturesBotKraken_9 import KrakenFuturesAutoTrader, KrakenFuturesClient as ClientTrader
    from FuturesProfitMonitor_6 import KrakenFuturesProfitMonitor
except ImportError as e:
    print(f"❌ Error importando scripts: {e}")
    print("   Asegúrate de que los tres archivos estén en la misma carpeta.")
    sys.exit(1)


# ╔══════════════════════════════════════════════════════════════╗
# ║              CONFIGURACIÓN DEL ORQUESTADOR                   ║
# ╚══════════════════════════════════════════════════════════════╝

WAIT_IF_NO_TRADES_SEC   = 30    # Espera si no hay señales antes de reintentar
WAIT_BETWEEN_CYCLES_SEC = 200   # Pausa entre el cierre del Bot2 y el inicio del Bot1
MAX_CYCLES              = None  # None = infinito

# Tiempo máximo de espera tras Bot1 para que Bot2 cierre (seg). None = esperar indefinidamente.
MONITOR_TIMEOUT_SEC     = None


# ╔══════════════════════════════════════════════════════════════╗
# ║                     ORQUESTADOR                              ║
# ╚══════════════════════════════════════════════════════════════╝

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[ORQUESTADOR {ts}] {msg}")


def check_has_open_positions() -> bool:
    try:
        from FuturesProfitMonitor_6 import KrakenFuturesClient as ClientMonitor
        client = ClientMonitor()
        result = client.get_open_positions()
        return len(result.get("openPositions", [])) > 0
    except Exception as e:
        log(f"⚠️  No se pudo verificar posiciones: {e}")
        return False


def run_bot1_until_trades(cycle: int) -> int:
    log("🤖 Iniciando BOT 1 — Buscando señales y abriendo órdenes...")
    try:
        trader = KrakenFuturesAutoTrader(cycle=cycle)
        if _TG_AVAILABLE:
            tg.notify_bot_start(cycle, trader._balance_usd)
        trader.run()
        trades_placed = len(trader.trade_log)
        log(f"✅ BOT 1 terminó — {trades_placed} trade(s) abierto(s).")
        return trades_placed
    except Exception as e:
        log(f"❌ BOT 1 error: {e}")
        if _TG_AVAILABLE:
            tg.notify_error("BOT 1 (run_bot1_until_trades)", str(e), cycle)
        return 0


def main():
    print("""
    ╔══════════════════════════════════════════════════════════════╗
    ║         KRAKEN FUTURES ORCHESTRATOR  (paralelo)              ║
    ║  Bot2 monitor arranca → Bot1 escanea en paralelo             ║
    ║  → objetivo alcanzado en cualquier momento → cierre          ║
    ╚══════════════════════════════════════════════════════════════╝
    """)
    print(f"Ciclos máximos  : {'∞ (infinito)' if MAX_CYCLES is None else MAX_CYCLES}")
    print(f"Espera sin señal: {WAIT_IF_NO_TRADES_SEC}s | Pausa entre ciclos: {WAIT_BETWEEN_CYCLES_SEC}s")
    print("\nPresiona Ctrl+C en cualquier momento para detener.\n")

    if _TG_AVAILABLE:
        tg.start_polling()
        tg.notify_orchestrator_start(MAX_CYCLES)

    cycle = 0
    active_monitor = None
    monitor_thread = None

    try:
        while MAX_CYCLES is None or cycle < MAX_CYCLES:

            if _TG_AVAILABLE and tg.STOP_FLAG:
                log("⏹️  [TG] Señal de parada recibida vía Telegram — deteniendo orquestador.")
                break

            cycle += 1
            log("=" * 50)
            log(f"🔄 CICLO #{cycle} COMENZANDO")
            log("=" * 50)

            # ── ARRANCAR MONITOR EN HILO DE FONDO ─────────────────────
            # Arranca ANTES que Bot1 para vigilar desde el primer tick.
            # Si no hay posiciones aún, el monitor espera pacientemente.
            log("💰 Arrancando BOT 2 (monitor) en hilo paralelo...")
            try:
                monitor = KrakenFuturesProfitMonitor(cycle=cycle)
                active_monitor = monitor
            except Exception as e:
                log(f"❌ No se pudo iniciar el monitor: {e}")
                if _TG_AVAILABLE:
                    tg.notify_error("Monitor init", str(e), cycle)
                cycle -= 1
                time.sleep(WAIT_IF_NO_TRADES_SEC)
                continue

            monitor_done = threading.Event()

            def _monitor_worker(mon=monitor, done=monitor_done, cyc=cycle):
                try:
                    mon.run()
                except Exception as exc:
                    log(f"❌ BOT 2 error en hilo: {exc}")
                    if _TG_AVAILABLE:
                        tg.notify_error("BOT 2 (hilo monitor)", str(exc), cyc)
                finally:
                    done.set()

            monitor_thread = threading.Thread(
                target=_monitor_worker, daemon=True, name=f"Monitor-{cycle}"
            )
            monitor_thread.start()
            log(f"✅ Monitor corriendo en paralelo (hilo: {monitor_thread.name})")

            # ── FASE 1: Bot1 escanea y abre órdenes ───────────────────
            trades = run_bot1_until_trades(cycle)

            # ¿Terminó el monitor mientras Bot1 escaneaba?
            if monitor_done.is_set():
                log("🎉 Monitor terminó mientras Bot1 escaneaba — objetivo alcanzado ya.")
                if _TG_AVAILABLE and cycle % 10 == 0:
                    tg.notify_daily_summary()
                if WAIT_BETWEEN_CYCLES_SEC > 0:
                    log(f"⏸️  Pausa de {WAIT_BETWEEN_CYCLES_SEC}s antes del siguiente ciclo...")
                    time.sleep(WAIT_BETWEEN_CYCLES_SEC)
                continue

            # ── Sin trades ni posiciones → reintentar ─────────────────
            if trades == 0:
                if check_has_open_positions():
                    log("ℹ️  Bot1 no abrió nuevas órdenes, pero hay posiciones previas. "
                        "Monitor ya las está vigilando.")
                else:
                    log(f"⏳ No hay señales ni posiciones. "
                        f"Deteniendo monitor y reintentando en {WAIT_IF_NO_TRADES_SEC}s...")
                    monitor.stop()
                    monitor_done.wait(timeout=10)
                    cycle -= 1
                    time.sleep(WAIT_IF_NO_TRADES_SEC)
                    continue

            # Chequear STOP_FLAG tras Bot1
            if _TG_AVAILABLE and tg.STOP_FLAG:
                log("⏹️  [TG] Señal de parada — deteniendo monitor y orquestador.")
                monitor.stop()
                monitor_done.wait(timeout=15)
                break

            # ── FASE 2: Esperar a que el monitor confirme cierre total ─
            log("⏳ Bot1 terminó. Esperando a que el monitor cierre todas las posiciones...")
            monitor_done.wait(timeout=MONITOR_TIMEOUT_SEC)

            if MONITOR_TIMEOUT_SEC and not monitor_done.is_set():
                log(f"⚠️  El monitor superó el timeout de {MONITOR_TIMEOUT_SEC}s — continuando.")

            log("✅ BOT 2 terminó — objetivo alcanzado o monitor detenido.")

            if _TG_AVAILABLE and cycle % 10 == 0:
                tg.notify_daily_summary()

            if WAIT_BETWEEN_CYCLES_SEC > 0:
                log(f"⏸️  Pausa de {WAIT_BETWEEN_CYCLES_SEC}s antes del siguiente ciclo...")
                time.sleep(WAIT_BETWEEN_CYCLES_SEC)

    except KeyboardInterrupt:
        log("⏹️  Orquestador detenido por el usuario.")
        if active_monitor is not None:
            log("   Enviando señal de parada al monitor activo...")
            active_monitor.stop()
            if monitor_thread and monitor_thread.is_alive():
                monitor_thread.join(timeout=10)

    finally:
        if _TG_AVAILABLE:
            tg.notify_orchestrator_stopped(cycle)
            tg.notify_daily_summary()
            tg.stop_polling()
        print("\n¡Hasta luego!\n")


if __name__ == "__main__":
    main()
