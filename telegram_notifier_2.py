"""
Telegram Notifier — Kraken Futures Bot
========================================
✅ Mensajes con botones Inline Keyboard
✅ Polling en hilo daemon (no bloquea el bot)
✅ Botones: ⏹ Parar Bot | 🔒 Cerrar Todo | 🔒 Cerrar posición individual | 📊 Ver posiciones
✅ FLAGS globales leídas por los scripts principales
✅ Handlers registrables sin imports circulares
"""

import requests
import time
import threading
from datetime import datetime
from typing import Optional, Callable, List, Dict

# ── Credenciales ──────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = "8207437880:AAGcgdbjTbTzdAUKyZLEBLqddfxK8dvTyE8"
TELEGRAM_CHAT_ID = "5825443798"
BASE_URL         = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  FLAGS GLOBALES  —  Los scripts leen estas variables en sus bucles          ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

STOP_FLAG      = False   # True → detener orquestador / monitor al final del ciclo
CLOSE_ALL_FLAG = False   # True → cerrar todas las posiciones ahora

# ── Handlers externos (se registran desde orchestrator / monitor) ─────────────
_close_all_handler:  Optional[Callable[[], None]]                     = None
_close_one_handler:  Optional[Callable[[str, str, float], None]]      = None   # (symbol, side, size)
_get_positions_fn:   Optional[Callable[[], List[Dict]]]               = None

# ── Estado del polling ────────────────────────────────────────────────────────
_poll_thread:  Optional[threading.Thread] = None
_poll_running  = False
_update_offset = 0

# ── Estado diario ─────────────────────────────────────────────────────────────
_daily_pnl      = 0.0
_daily_trades   = 0
_daily_date     = datetime.now().strftime("%Y-%m-%d")
_last_cycle_pnl = 0.0


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  REGISTRO DE HANDLERS                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def register_handlers(
    close_all:     Optional[Callable[[], None]]                 = None,
    close_one:     Optional[Callable[[str, str, float], None]]  = None,
    get_positions: Optional[Callable[[], List[Dict]]]           = None,
):
    """
    Llamar desde FuturesProfitMonitor / orchestrator al arrancar.

    Ejemplo:
        tg.register_handlers(
            close_all    = monitor.close_all_via_telegram,
            close_one    = monitor.close_one_via_telegram,
            get_positions= monitor.get_open_positions,
        )
    """
    global _close_all_handler, _close_one_handler, _get_positions_fn
    if close_all:      _close_all_handler  = close_all
    if close_one:      _close_one_handler  = close_one
    if get_positions:  _get_positions_fn   = get_positions


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  ENVÍO BASE                                                                 ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _post(endpoint: str, payload: dict, retries: int = 3) -> Optional[dict]:
    url = f"{BASE_URL}/{endpoint}"
    for attempt in range(retries):
        try:
            r = requests.post(url, json=payload, timeout=10)
            if r.status_code == 200:
                return r.json()
        except Exception:
            if attempt < retries - 1:
                time.sleep(2)
    return None


def send(message: str, reply_markup: Optional[dict] = None, retries: int = 3) -> Optional[int]:
    """Envía un mensaje. Devuelve el message_id para poder editarlo después."""
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "text":       message,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    result = _post("sendMessage", payload, retries)
    if result and result.get("ok"):
        return result["result"]["message_id"]
    return None


def _edit_message(message_id: int, new_text: str, reply_markup: Optional[dict] = None):
    """Edita un mensaje ya enviado (p.ej. para deshabilitar los botones)."""
    payload = {
        "chat_id":    TELEGRAM_CHAT_ID,
        "message_id": message_id,
        "text":       new_text,
        "parse_mode": "HTML",
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    _post("editMessageText", payload)


def _answer_callback(callback_id: str, text: str = "", alert: bool = False):
    _post("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text":              text,
        "show_alert":        alert,
    })


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  TECLADOS INLINE                                                            ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _main_keyboard() -> dict:
    """Teclado de control principal adjunto a notificaciones importantes."""
    return {
        "inline_keyboard": [
            [
                {"text": "⏹ Parar Bot",    "callback_data": "cmd:stop"},
                {"text": "🔒 Cerrar Todo", "callback_data": "cmd:close_all"},
            ],
            [
                {"text": "📊 Ver Posiciones", "callback_data": "cmd:list"},
                {"text": "⏱ Estado",          "callback_data": "cmd:status"},
            ],
        ]
    }


def _positions_keyboard(positions: List[Dict]) -> dict:
    """Un botón por posición + fila inferior con acciones globales."""
    rows = []
    for p in positions:
        arrow = "📈" if p["side"] == "long" else "📉"
        label = f"{arrow} {p['symbol']} {p['side'].upper()}"
        data  = f"cmd:close_one:{p['symbol']}:{p['side']}:{int(p['size'])}"
        rows.append([{"text": label, "callback_data": data}])
    rows.append([
        {"text": "🔒 Cerrar Todo", "callback_data": "cmd:close_all"},
        {"text": "⏹ Parar Bot",   "callback_data": "cmd:stop"},
    ])
    rows.append([
        {"text": "⏱ Estado",      "callback_data": "cmd:status"},
    ])
    return {"inline_keyboard": rows}


def _done_keyboard(label: str = "✅ Ejecutado") -> dict:
    """Reemplaza los botones tras ejecutar una acción."""
    return {"inline_keyboard": [[{"text": label, "callback_data": "cmd:noop"}]]}


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  HANDLER DE CALLBACKS                                                       ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _handle_callback(cb: dict):
    global STOP_FLAG, CLOSE_ALL_FLAG

    cb_id  = cb["id"]
    data   = cb.get("data", "")
    msg_id = cb["message"]["message_id"]
    ts     = datetime.now().strftime("%H:%M:%S")

    # ── Noop (botón ya procesado) ──────────────────────────────────────────────
    if data == "cmd:noop":
        _answer_callback(cb_id, "Ya ejecutado")
        return

    # ── ⏹ Parar Bot ───────────────────────────────────────────────────────────
    if data == "cmd:stop":
        STOP_FLAG = True
        _answer_callback(cb_id, "⏹ Señal de parada enviada", alert=True)
        _edit_message(msg_id,
            f"⏹ <b>PARADA SOLICITADA</b> vía Telegram\n🕐 {ts}",
            _done_keyboard("⏹ Bot deteniéndose…"))
        send(
            f"⏹ <b>BOT DETENIÉNDOSE</b>\n"
            f"🕐 {ts}\n"
            f"El orquestador finalizará el ciclo actual y se detendrá."
        )
        return

    # ── 🔒 Cerrar Todo ─────────────────────────────────────────────────────────
    if data == "cmd:close_all":
        _answer_callback(cb_id, "🔒 Cerrando todas las posiciones…", alert=True)
        _edit_message(msg_id,
            f"🔒 <b>CIERRE TOTAL solicitado</b> vía Telegram\n🕐 {ts}",
            _done_keyboard("🔒 Cerrando…"))
        if _close_all_handler:
            try:
                _close_all_handler()
                send(
                    f"✅ <b>Todas las posiciones cerradas</b>\n"
                    f"🕐 {datetime.now().strftime('%H:%M:%S')}",
                    _main_keyboard(),
                )
            except Exception as e:
                send(f"❌ Error cerrando posiciones: <code>{e}</code>")
        else:
            # Handler aún no registrado → activar flag para que el loop lo ejecute
            CLOSE_ALL_FLAG = True
            send(
                f"🔒 <b>CIERRE TOTAL</b> programado\n"
                f"🕐 {ts}\n"
                f"El monitor cerrará las posiciones en el próximo ciclo."
            )
        return

    # ── 🔒 Cerrar posición individual ──────────────────────────────────────────
    # formato: "cmd:close_one:PF_XBTUSD:long:10"
    if data.startswith("cmd:close_one:"):
        parts = data.split(":")
        if len(parts) >= 5:
            symbol = parts[2]
            side   = parts[3]
            size   = float(parts[4])
            _answer_callback(cb_id, f"🔒 Cerrando {symbol}…", alert=True)
            _edit_message(msg_id,
                f"🔒 Cerrando <b>{symbol}</b> ({side.upper()})…\n🕐 {ts}",
                _done_keyboard(f"🔒 {symbol} cerrando…"))
            if _close_one_handler:
                try:
                    _close_one_handler(symbol, side, size)
                    send(
                        f"✅ <b>{symbol}</b> ({side.upper()}) cerrado\n"
                        f"🕐 {datetime.now().strftime('%H:%M:%S')}",
                        _main_keyboard(),
                    )
                except Exception as e:
                    send(f"❌ Error cerrando {symbol}: <code>{e}</code>")
            else:
                send(
                    f"⚠️ Handler individual no registrado aún.\n"
                    f"Usa 🔒 <b>Cerrar Todo</b> para cerrar todas las posiciones."
                )
        return

    # ── 📊 Ver Posiciones ──────────────────────────────────────────────────────
    if data == "cmd:list":
        _answer_callback(cb_id, "📊 Consultando posiciones…")
        if _get_positions_fn:
            try:
                positions = _get_positions_fn()
                if not positions:
                    send("📊 <b>Sin posiciones abiertas</b>", _main_keyboard())
                    return
                total_pnl = sum(p["pnl"] for p in positions)
                lines = [f"📊 <b>POSICIONES ABIERTAS ({len(positions)})</b>\n🕐 {ts}\n"]
                for p in positions:
                    arrow = "📈" if p["side"] == "long" else "📉"
                    emoji = "🟢" if p["pnl"] >= 0 else "🔴"
                    lines.append(
                        f"{emoji} {arrow} <b>{p['symbol']}</b> {p['side'].upper()}\n"
                        f"   Entrada: ${p['entry']:,.4f} | Mark: ${p['mark']:,.4f}\n"
                        f"   PnL: <b>${p['pnl']:+.4f}</b>"
                    )
                lines.append(f"\n💰 <b>PnL TOTAL: ${total_pnl:+.4f} USD</b>")
                send("\n".join(lines), _positions_keyboard(positions))
            except Exception as e:
                send(f"❌ Error obteniendo posiciones: <code>{e}</code>")
        else:
            send("⚠️ Función de posiciones no registrada aún.", _main_keyboard())
        return

    # ── ⏱ Estado — tiempo abierto + PnL por posición ──────────────────────────
    if data == "cmd:status":
        _answer_callback(cb_id, "⏱ Consultando estado…")
        if _get_positions_fn:
            try:
                positions = _get_positions_fn()
                if not positions:
                    send("⏱ <b>Sin posiciones abiertas</b>", _main_keyboard())
                    return

                now = datetime.utcnow()
                total_pnl = sum(p["pnl"] for p in positions)
                lines = [f"⏱ <b>ESTADO DE POSICIONES ({len(positions)})</b>\n🕐 {ts}\n"]

                for p in positions:
                    arrow    = "📈" if p["side"] == "long" else "📉"
                    pnl_emoji = "🟢" if p["pnl"] >= 0 else "🔴"

                    # Calcular tiempo abierto desde fill_time
                    elapsed_str = "desconocido"
                    fill_time   = p.get("fill_time", "")
                    if fill_time:
                        try:
                            ft = datetime.strptime(fill_time[:19], "%Y-%m-%dT%H:%M:%S")
                            delta   = now - ft
                            total_s = int(delta.total_seconds())
                            hours   = total_s // 3600
                            minutes = (total_s % 3600) // 60
                            seconds = total_s % 60
                            if hours > 0:
                                elapsed_str = f"{hours}h {minutes:02d}m {seconds:02d}s"
                            else:
                                elapsed_str = f"{minutes}m {seconds:02d}s"
                        except Exception:
                            elapsed_str = fill_time[:19]

                    # PnL/min (rendimiento por minuto)
                    pnl_rate_str = ""
                    if fill_time:
                        try:
                            mins_open = max(delta.total_seconds() / 60, 1)
                            rate = p["pnl"] / mins_open
                            pnl_rate_str = f" ({rate:+.4f}/min)"
                        except Exception:
                            pass

                    lines.append(
                        f"{pnl_emoji} {arrow} <b>{p['symbol']}</b> {p['side'].upper()}\n"
                        f"   ⏱ Abierta: <b>{elapsed_str}</b>\n"
                        f"   📥 Entrada: ${p['entry']:,.4f} | Mark: ${p['mark']:,.4f}\n"
                        f"   💰 PnL: <b>${p['pnl']:+.4f}</b>{pnl_rate_str}"
                    )

                pnl_emoji_total = "🟢" if total_pnl >= 0 else "🔴"
                lines.append(f"\n{pnl_emoji_total} <b>PnL TOTAL: ${total_pnl:+.4f} USD</b>")
                send("\n".join(lines), _main_keyboard())
            except Exception as e:
                send(f"❌ Error obteniendo estado: <code>{e}</code>")
        else:
            send("⚠️ Función de posiciones no registrada aún.", _main_keyboard())
        return


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  POLLING (hilo daemon)                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _polling_loop():
    global _poll_running, _update_offset
    _poll_running = True
    while _poll_running:
        try:
            r = requests.get(
                f"{BASE_URL}/getUpdates",
                params={
                    "offset":          _update_offset,
                    "timeout":         20,
                    "allowed_updates": ["callback_query"],
                },
                timeout=25,
            )
            if r.status_code == 200:
                for update in r.json().get("result", []):
                    _update_offset = update["update_id"] + 1
                    if "callback_query" in update:
                        try:
                            _handle_callback(update["callback_query"])
                        except Exception as e:
                            print(f"[TG] Error en callback: {e}")
        except Exception:
            time.sleep(5)


def start_polling():
    """Arranca el hilo de polling. Llamar UNA VEZ al inicio del script principal."""
    global _poll_thread, _poll_running
    if _poll_thread and _poll_thread.is_alive():
        return
    _poll_thread = threading.Thread(
        target=_polling_loop, daemon=True, name="TelegramPoller"
    )
    _poll_thread.start()
    print("✅ [TG] Polling Telegram iniciado (hilo daemon)")


def stop_polling():
    global _poll_running
    _poll_running = False


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  ESTADO DIARIO                                                              ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def _reset_daily_if_needed():
    global _daily_pnl, _daily_trades, _daily_date
    today = datetime.now().strftime("%Y-%m-%d")
    if today != _daily_date:
        _daily_pnl    = 0.0
        _daily_trades = 0
        _daily_date   = today


def add_daily_pnl(amount: float):
    global _daily_pnl, _daily_trades
    _reset_daily_if_needed()
    _daily_pnl    += amount
    _daily_trades += 1


def get_daily_stats() -> dict:
    _reset_daily_if_needed()
    return {"date": _daily_date, "pnl": _daily_pnl, "trades": _daily_trades}


def set_last_cycle_pnl(pnl: float):
    global _last_cycle_pnl
    _last_cycle_pnl = pnl


def get_last_cycle_pnl() -> float:
    return _last_cycle_pnl


# ╔══════════════════════════════════════════════════════════════════════════════╗
# ║  MENSAJES PREDEFINIDOS                                                      ║
# ╚══════════════════════════════════════════════════════════════════════════════╝

def notify_bot_start(cycle: int, balance: float):
    _reset_daily_if_needed()
    stats = get_daily_stats()
    send(
        f"🤖 <b>BOT INICIADO — Ciclo #{cycle}</b>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
        f"💵 Balance FLEX: <b>${balance:,.2f} USD</b>\n"
        f"📅 PnL del día ({stats['date']}): <b>${stats['pnl']:+.4f} USD</b> "
        f"({stats['trades']} cierres)",
        _main_keyboard(),
    )


def notify_order_opened(symbol: str, side: str, size: int, price: float,
                        sl: float, tp: float, notional: float, cycle: int):
    arrow = "📈 LONG" if side == "buy" else "📉 SHORT"
    send(
        f"🟢 <b>ORDEN ABIERTA — Ciclo #{cycle}</b>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
        f"{arrow} <b>{symbol}</b>\n"
        f"📦 Contratos: {size} | Precio: ${price:,.4f}\n"
        f"💰 Nocional: ~${notional:,.2f} USD\n"
        f"🛑 SL: ${sl:,.4f} | 🎯 TP: ${tp:,.4f}",
        _main_keyboard(),
    )


def notify_orders_summary(trades: list, cycle: int):
    if not trades:
        send(
            f"⚪ <b>Sin señales — Ciclo #{cycle}</b>\n"
            f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
            f"No se abrieron posiciones en este ciclo.",
            _main_keyboard(),
        )
        return
    lines = [
        f"📋 <b>RESUMEN APERTURA — Ciclo #{cycle}</b>",
        f"🕐 {datetime.now().strftime('%H:%M:%S')}",
        f"Posiciones abiertas: <b>{len(trades)}</b>\n",
    ]
    for t in trades:
        arrow = "📈" if t["side"] == "buy" else "📉"
        lines.append(
            f"{arrow} {t['symbol']} | {t['side'].upper()} | "
            f"${t['price']:,.4f} | ~${t['notional_usd']:,.2f}"
        )
    send("\n".join(lines), _main_keyboard())


def notify_positions_closed(positions: list, total_pnl: float, cycle: int):
    stats = get_daily_stats()
    lines = [
        f"🔒 <b>OBJETIVO ALCANZADO — Ciclo #{cycle}</b>",
        f"🕐 {datetime.now().strftime('%H:%M:%S')}",
        f"",
        f"💰 PnL este cierre : <b>${total_pnl:+.4f} USD</b>",
        f"📅 PnL del día     : <b>${stats['pnl']:+.4f} USD</b>",
        f"🔢 Cierres hoy     : {stats['trades']}",
        f"",
    ]
    for p in positions:
        emoji = "✅" if p.get("ok", True) else "❌"
        arrow = "📈" if p["side"] == "long" else "📉"
        lines.append(f"{emoji} {arrow} {p['symbol']} | {p['side'].upper()} | PnL: ${p['pnl']:+.4f}")
    send("\n".join(lines), _main_keyboard())


def notify_position_closed_single(symbol: str, side: str, pnl: float,
                                   total_pnl: float, cycle: int, ok: bool = True):
    emoji = "✅" if ok else "❌"
    arrow = "📈 LONG" if side == "long" else "📉 SHORT"
    stats = get_daily_stats()
    send(
        f"{emoji} <b>POSICIÓN CERRADA</b>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')} | Ciclo #{cycle}\n"
        f"{arrow} <b>{symbol}</b>\n"
        f"💰 PnL posición : <b>${pnl:+.4f} USD</b>\n"
        f"💰 PnL total    : <b>${total_pnl:+.4f} USD</b>\n"
        f"📅 PnL del día  : <b>${stats['pnl']:+.4f} USD</b>",
        _main_keyboard(),
    )


def notify_old_position_closed(symbol: str, side: str, age_hours: float, pnl: float):
    send(
        f"⏰ <b>POSICIÓN CERRADA POR TIEMPO</b>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
        f"📍 {symbol} | {side.upper()}\n"
        f"⌛ Antigüedad: {age_hours:.1f}h\n"
        f"💰 PnL aprox: ${pnl:+.4f} USD",
        _main_keyboard(),
    )


def notify_error(context: str, error: str, cycle: Optional[int] = None):
    cycle_str = f" — Ciclo #{cycle}" if cycle else ""
    send(
        f"❌ <b>ERROR{cycle_str}</b>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
        f"📍 Contexto: {context}\n"
        f"⚠️ Error: <code>{str(error)[:300]}</code>"
    )


def notify_margin_warning(margin: float, cycle: int):
    send(
        f"🚨 <b>ALERTA DE MARGEN — Ciclo #{cycle}</b>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
        f"⚠️ Nivel de margen crítico: <b>{margin:.1f}%</b> (mín. 100%)\n"
        f"🛑 Bot detenido para proteger la cuenta.",
        _main_keyboard(),
    )


def notify_daily_summary():
    stats = get_daily_stats()
    emoji = "🟢" if stats["pnl"] >= 0 else "🔴"
    send(
        f"{emoji} <b>RESUMEN DIARIO — {stats['date']}</b>\n"
        f"💰 PnL total del día : <b>${stats['pnl']:+.4f} USD</b>\n"
        f"🔢 Ciclos cerrados   : {stats['trades']}\n"
        f"📊 Promedio/ciclo    : "
        f"${(stats['pnl'] / stats['trades'] if stats['trades'] else 0):+.4f} USD",
        _main_keyboard(),
    )


def notify_orchestrator_start(max_cycles):
    send(
        f"🚀 <b>ORQUESTADOR INICIADO</b>\n"
        f"🕐 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"🔄 Ciclos máximos: {'∞' if max_cycles is None else max_cycles}\n"
        f"⚙️  Bot1 (abrir) → Bot2 (cerrar) → bucle",
        _main_keyboard(),
    )


def notify_orchestrator_stopped(total_cycles: int):
    stats = get_daily_stats()
    send(
        f"⏹️ <b>ORQUESTADOR DETENIDO</b>\n"
        f"🕐 {datetime.now().strftime('%H:%M:%S')}\n"
        f"🔄 Ciclos completados : {total_cycles}\n"
        f"💰 PnL del día        : <b>${stats['pnl']:+.4f} USD</b>\n"
        f"🔢 Cierres totales    : {stats['trades']}"
    )
