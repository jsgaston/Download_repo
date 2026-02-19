"""
Kraken Futures Profit Monitor
==============================
✅ Config hardcodeada (sin JSON externo)
✅ Monitorea posiciones PF_ en tiempo real
✅ Cierra todo al alcanzar el objetivo en USD
✅ Cancela SL/TP al cerrar
✅ Objetivo fijo en USD o % del balance
"""

import hashlib, hmac, base64, time, urllib.parse, threading
import requests
from datetime import datetime
from typing import Dict, List
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    import telegram_notifier as tg
    _TG_AVAILABLE = True
except ImportError:
    _TG_AVAILABLE = False


# ╔══════════════════════════════════════════════════════════════╗
# ║                     CONFIGURACIÓN                            ║
# ╚══════════════════════════════════════════════════════════════╝

API_KEY    = "Qh9s+qkIjZXgLTTmSA6IcqgrpbZR/Ep7gqKVlYIiPAx7EC2iSLE5A5Hi"
API_SECRET = "aDe8x9pG+uQ/O2+izP0t6q7joPhPLlcxnTABYdo5tXs9B54k4m4/moLRyTDKVMGjBetcT0n1YTcXmJDQISWjeHLP"

# ── Objetivo de ganancia ──────────────────────────────────────
PROFIT_TARGET_USD   = 2.0         # Cerrar cuando ganes $5 USD
PROFIT_TARGET_PCT   = None        # Alternativa: 2.0 = 2% del balance (None = desactivado)

# ── Comportamiento ────────────────────────────────────────────
CHECK_INTERVAL_SEC  = 3         # Segundos entre cada chequeo
CLOSE_ALL_ON_TARGET = True        # True = cierra todo | False = solo avisa
SHOW_POSITIONS      = True        # True = detalle por posición | False = solo barra progreso
CANCEL_ORDERS       = True        # Cancelar SL/TP pendientes al cerrar posiciones
PNL_CHART_WIDTH     = 60          # Puntos visibles en el grafico historico de PnL
TG_CHART_INTERVAL   = 180         # Segundos entre envios del grafico por Telegram (180 = 3 min)


# ╔══════════════════════════════════════════════════════════════╗
# ║                  CLIENTE KRAKEN FUTURES                      ║
# ╚══════════════════════════════════════════════════════════════╝

class KrakenFuturesClient:

    BASE_URL = "https://futures.kraken.com"

    def _sign(self, endpoint: str, post_data: str, nonce: str) -> str:
        path   = endpoint.replace("/derivatives", "")
        msg    = (post_data + nonce + path).encode("utf-8")
        sha256 = hashlib.sha256(msg).digest()
        secret = base64.b64decode(API_SECRET)
        sig    = hmac.new(secret, sha256, hashlib.sha512).digest()
        return base64.b64encode(sig).decode()

    def _request(self, method: str, endpoint: str, params: dict = None) -> dict:
        nonce     = str(int(time.time() * 1000))
        post_data = urllib.parse.urlencode(params) if params else ""
        url       = self.BASE_URL + endpoint
        signed_pd = ""

        if method == "GET" and params:
            url = self.BASE_URL + endpoint + "?" + post_data
        elif method == "POST":
            signed_pd = post_data

        headers = {"APIKey": API_KEY, "Authent": self._sign(endpoint, signed_pd, nonce), "Nonce": nonce}
        if method == "POST":
            headers["Content-Type"] = "application/x-www-form-urlencoded"
            return requests.post(url, data=post_data, headers=headers, timeout=15).json()
        return requests.get(url, headers=headers, timeout=15).json()

    def get_accounts(self):
        return self._request("GET", "/derivatives/api/v3/accounts")

    def get_open_positions(self):
        return self._request("GET", "/derivatives/api/v3/openpositions")

    def get_tickers(self):
        return requests.get(self.BASE_URL + "/derivatives/api/v3/tickers", timeout=15).json()

    def send_order(self, order_type, symbol, side, size, reduce_only=True):
        p = {"orderType": order_type, "symbol": symbol, "side": side, "size": int(size)}
        if reduce_only: p["reduceOnly"] = "true"
        return self._request("POST", "/derivatives/api/v3/sendorder", p)

    def cancel_all_orders(self, symbol=None):
        return self._request("POST", "/derivatives/api/v3/cancelallorders",
                             {"symbol": symbol} if symbol else {})

    def get_fills(self, last_fill_time: str = None):
        """Devuelve fills recientes. Usado para detectar motivo de cierre."""
        params = {}
        if last_fill_time:
            params["lastFillTime"] = last_fill_time
        return self._request("GET", "/derivatives/api/v3/fills", params or None)

    def get_open_orders(self):
        return self._request("GET", "/derivatives/api/v3/openorders")


# ╔══════════════════════════════════════════════════════════════╗
# ║                  PROFIT MONITOR                              ║
# ╚══════════════════════════════════════════════════════════════╝

class KrakenFuturesProfitMonitor:

    def __init__(self, cycle: int = 1):
        self.client       = KrakenFuturesClient()
        self._price_cache = {}
        self._cache_ts    = 0
        self.cycle        = cycle

        result = self.client.get_accounts()
        if result.get("result") != "success":
            err = result.get("error")
            if _TG_AVAILABLE: tg.notify_error("KrakenFuturesProfitMonitor.__init__", err, cycle)
            raise Exception(f"Auth fallida: {err}")

        flex = result["accounts"].get("flex", {})
        self.balance_usd  = flex.get("currencies", {}).get("USD", {}).get("available", 0)
        self.unrealized   = flex.get("totalUnrealized", 0)

        print(f"✅ Conectado — Saldo FLEX: ${self.balance_usd:.2f} USD | PnL unrealizado: ${self.unrealized:+.2f}")

        # Tracking de posiciones para detectar cierres externos
        self._prev_positions: Dict[str, Dict] = {}   # key: symbol:side
        self._last_fill_time: str = ""

        # Historial de PnL para el grafico ASCII
        self._pnl_history: List[tuple] = []   # (datetime, pnl_float)

        # Control del hilo de envio periodico a Telegram
        self._chart_stop   = threading.Event()
        self._chart_thread = None

        # Flag para parada externa desde el orquestador
        self._external_stop = threading.Event()

    def stop(self):
        """Señaliza al monitor que debe terminar su loop en el próximo tick."""
        self._external_stop.set()

    def resolve_target(self) -> float:
        if PROFIT_TARGET_PCT:
            return self.balance_usd * PROFIT_TARGET_PCT / 100
        return PROFIT_TARGET_USD

    # ── Precios ───────────────────────────────────────────────

    def _refresh_prices(self):
        if time.time() - self._cache_ts < 5: return
        try:
            for t in self.client.get_tickers().get("tickers", []):
                self._price_cache[t["symbol"]] = float(t.get("markPrice") or t.get("last") or 0)
            self._cache_ts = time.time()
        except Exception: pass

    def get_mark_price(self, symbol: str) -> float:
        self._refresh_prices()
        return self._price_cache.get(symbol, 0.0)

    # ── Posiciones y P&L ─────────────────────────────────────

    def get_open_positions(self) -> List[Dict]:
        try:
            positions = []
            for p in self.client.get_open_positions().get("openPositions", []):
                symbol = p["symbol"]
                side   = p["side"]
                size   = float(p["size"])
                entry  = float(p["price"])
                mark   = self.get_mark_price(symbol)

                # P&L en USD (perpetuos lineales: 1 contrato = $1 nocional)
                if mark > 0 and entry > 0:
                    pnl = (mark - entry) * size if side == "long" else (entry - mark) * size
                else:
                    pnl = 0.0

                positions.append({
                    "symbol": symbol, "side": side,
                    "size": size, "entry": entry,
                    "mark": mark, "pnl": pnl,
                    "fill_time": p.get("fillTime", ""),
                })
            return positions
        except Exception as e:
            print(f"Error posiciones: {e}"); return []

    def total_pnl(self, positions: List[Dict]) -> float:
        return sum(p["pnl"] for p in positions)

    # ── Cierre ────────────────────────────────────────────────



    # ── Detección de cierres externos ──────────────────────────

    def _detect_close_reason(self, symbol: str, side: str) -> str:
        """
        Consulta fills recientes para determinar por qué se cerró una posición:
        - SL (stop order ejecutado)
        - TP (take_profit ejecutado)
        - Manual (orden de mercado/límite no reduceOnly del bot)
        - Desconocido
        """
        try:
            result = self.client.get_fills(self._last_fill_time or None)
            fills  = result.get("fills", [])
            # Buscar el fill más reciente para este símbolo en dirección de cierre
            close_side = "sell" if side == "long" else "buy"
            relevant = [
                f for f in fills
                if f.get("symbol") == symbol and f.get("side") == close_side
            ]
            if not relevant:
                return "Desconocido"
            # Ordenar por tiempo descendente y coger el más reciente
            relevant.sort(key=lambda x: x.get("fillTime", ""), reverse=True)
            fill = relevant[0]
            order_type = fill.get("orderType", "").lower()
            if "stp" in order_type or "stop" in order_type:
                return "🛑 SL tocado"
            if "take_profit" in order_type or "takeprofit" in order_type:
                return "🎯 TP tocado"
            if order_type in ("mkt", "lmt", "ioc", "post"):
                return "✋ Cierre manual"
            return f"❓ Desconocido ({order_type})"
        except Exception as e:
            return f"❓ Error consultando fills: {e}"

    def _update_fill_time(self):
        """Actualiza la marca de tiempo del último fill para limitar consultas."""
        try:
            from datetime import timezone
            self._last_fill_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        except Exception:
            pass

    def check_external_closes(self, current_positions: List[Dict]) -> List[Dict]:
        """
        Compara posiciones actuales con las del ciclo anterior.
        Devuelve lista de posiciones que desaparecieron (cerradas externamente).
        """
        current_keys = {f"{p['symbol']}:{p['side']}" for p in current_positions}
        closed_externally = []
        for key, pos in self._prev_positions.items():
            if key not in current_keys:
                symbol, side = key.split(":", 1)
                reason = self._detect_close_reason(symbol, side)
                closed_externally.append({**pos, "reason": reason})
        # Actualizar snapshot
        self._prev_positions = {f"{p['symbol']}:{p['side']}": p for p in current_positions}
        return closed_externally

    def _close_one(self, p: Dict) -> Dict:
        opp = "sell" if p["side"] == "long" else "buy"
        r   = self.client.send_order("mkt", p["symbol"], opp, int(p["size"]))
        ok  = r.get("result") == "success"
        if ok and CANCEL_ORDERS:
            self.client.cancel_all_orders(p["symbol"])
        return {"symbol": p["symbol"], "side": p["side"], "pnl": p["pnl"], "ok": ok}

    def close_all(self, positions: List[Dict]) -> int:
        print(f"\n🔒 Cerrando {len(positions)} posición(es) en paralelo...")
        closed = 0
        results_for_tg = []
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = {executor.submit(self._close_one, p): p for p in positions}
            for f in as_completed(futures):
                res = f.result()
                emoji = "✅" if res["ok"] else "❌"
                print(f"   {emoji} {res['symbol']} ({res['side'].upper()}) | PnL: ${res['pnl']:+.4f}")
                results_for_tg.append(res)
                if res["ok"]:
                    closed += 1
        return closed, results_for_tg

    # ── Barra de progreso ─────────────────────────────────────

    def progress_bar(self, current: float, target: float, width: int = 30) -> str:
        pct    = min(max(current / target, 0), 1.0) if target > 0 else 0
        filled = int(width * pct)
        return "[" + "█" * filled + "░" * (width - filled) + "]"

    # ── Envio periodico del grafico por Telegram ──────────────

    def _chart_sender(self):
        while not self._chart_stop.wait(TG_CHART_INTERVAL):
            if not _TG_AVAILABLE or not self._pnl_history:
                continue
            try:
                positions = self.get_open_positions()
                pnl       = self.total_pnl(positions)
                target    = self.resolve_target()
                elapsed   = str(datetime.now() - self._pnl_history[0][0]).split(".")[0]
                chart_txt = self.pnl_chart()
                pnl_emoji = "\U0001f7e2" if pnl >= 0 else "\U0001f534"
                pct       = (pnl / target * 100) if target else 0
                msg = (
                    f"\U0001f4c9 <b>GRAFICO PnL \u2014 Ciclo #{self.cycle}</b>\n"
                    f"\U0001f550 {datetime.now().strftime('%H:%M:%S')} | "
                    f"Sesion: {elapsed} | Muestras: {len(self._pnl_history)}\n\n"
                    f"{pnl_emoji} PnL actual : <b>${pnl:+.4f} USD</b>\n"
                    f"\U0001f3af Objetivo   : ${target:.4f} USD\n"
                    f"\U0001f4ca Progreso   : {pct:.1f}%\n\n"
                    f"<pre>{chart_txt}</pre>"
                )
                tg.send(msg)
            except Exception as e:
                print(f"[TG-chart] Error: {e}")

    def _start_chart_thread(self):
        if not _TG_AVAILABLE:
            return
        self._chart_stop.clear()
        self._chart_thread = threading.Thread(
            target=self._chart_sender, daemon=True, name="ChartSender"
        )
        self._chart_thread.start()
        print(f"\u2705 [TG] Grafico Telegram cada {TG_CHART_INTERVAL}s")

    def _stop_chart_thread(self):
        self._chart_stop.set()

    def pnl_chart(self, width: int = None) -> str:
        """
        Grafico ASCII puro del PnL historico. Compatible con cualquier terminal.
        Filas de alturas 1-8 representadas con: . : - = + | # *
        """
        width   = width or PNL_CHART_WIDTH
        history = self._pnl_history[-width:]
        if len(history) < 2:
            return "   (acumulando datos...)"

        values  = [v for _, v in history]
        lo, hi  = min(values), max(values)
        span    = hi - lo if hi != lo else 1.0

        # 8 niveles de altura en ASCII puro
        LEVELS  = [".", ":", "-", "=", "+", "s", "#", "*"]

        def _char(v):
            idx = int((v - lo) / span * (len(LEVELS) - 1))
            return LEVELS[max(0, min(idx, len(LEVELS) - 1))]

        spark   = "".join(_char(v) for v in values)
        t_start = history[0][0].strftime("%H:%M")
        t_end   = history[-1][0].strftime("%H:%M")

        W   = len(spark)
        sep = "   +" + "-" * W + "+"

        lines = [
            sep,
            "   |" + spark + "|  max: ${:.4f}".format(hi),
        ]
        if lo < 0 < hi:
            zero_col = max(0, int((0 - lo) / span * W))
            zero_row = " " * zero_col + "|" + "-" * (W - zero_col - 1)
            lines.append("   |" + zero_row + "|  $0.0000")
        lines += [
            sep + "  min: ${:.4f}".format(lo),
            "   " + t_start + " " * max(0, W - len(t_start) - len(t_end)) + t_end,
        ]
        return "\n".join(lines)

    # __ Main loop _____________________________________________________________



    def run(self):
        target = self.resolve_target()
        self._update_fill_time()   # marca de tiempo de inicio para fills
        self._start_chart_thread() # hilo de envio periodico del grafico a Telegram

        print("\n" + "="*65)
        print("💰 KRAKEN FUTURES PROFIT MONITOR")
        print("="*65)
        if PROFIT_TARGET_PCT:
            print(f"Objetivo: {PROFIT_TARGET_PCT}% del balance = ${target:.2f} USD")
        else:
            print(f"Objetivo: ${target:.2f} USD")
        print(f"Intervalo: {CHECK_INTERVAL_SEC}s | Al alcanzar: {'Cerrar todo ✅' if CLOSE_ALL_ON_TARGET else 'Solo avisar 🔔'}")
        print("="*65)
        print("\n⏳ Monitoreando... (Ctrl+C para salir)\n")

        try:
            while not self._external_stop.is_set():
                positions = self.get_open_positions()

                # ── Detectar cierres externos (SL/TP/manual) ──────────
                closed_ext = self.check_external_closes(positions)
                for c in closed_ext:
                    arrow  = "📈" if c["side"] == "long" else "📉"
                    reason = c.get("reason", "Desconocido")
                    pnl_c  = c.get("pnl", 0.0)
                    msg    = (f"\n   ⚠️  POSICIÓN CERRADA EXTERNAMENTE\n"
                              f"      {arrow} {c['symbol']} ({c['side'].upper()}) "
                              f"| PnL aprox: ${pnl_c:+.4f} | Motivo: {reason}")
                    print(msg)
                    if _TG_AVAILABLE:
                        tg.send(
                            f"⚠️ <b>POSICIÓN CERRADA EXTERNAMENTE</b>\n"
                            f"🕐 {datetime.now().strftime('%H:%M:%S')} | Ciclo #{self.cycle}\n"
                            f"{arrow} <b>{c['symbol']}</b> ({c['side'].upper()})\n"
                            f"💰 PnL aprox: <b>${pnl_c:+.4f}</b>\n"
                            f"📌 Motivo: <b>{reason}</b>"
                        )

                pnl       = self.total_pnl(positions)
                ts        = datetime.now().strftime("%H:%M:%S")

                if not positions:
                    print(f"[{ts}] Sin posiciones abiertas — esperando...")
                    time.sleep(CHECK_INTERVAL_SEC)
                    continue

                # Registrar muestra en el historial
                self._pnl_history.append((datetime.now(), pnl))

                if SHOW_POSITIONS:
                    print(f"\n[{ts}] 📊 POSICIONES:")
                    for p in positions:
                        arrow = "📈" if p["side"] == "long" else "📉"
                        emoji = "🟢" if p["pnl"] >= 0 else "🔴"
                        print(f"   {emoji} {arrow} {p['symbol']} | "
                              f"entrada: ${p['entry']:,.2f} | mark: ${p['mark']:,.2f} | "
                              f"PnL: ${p['pnl']:+.4f}")
                    bar = self.progress_bar(pnl, target)
                    elapsed = str(datetime.now() - self._pnl_history[0][0]).split(".")[0]
                    print(f"\n   💰 PnL TOTAL : ${pnl:+.4f} USD")
                    print(f"   🎯 Objetivo  : ${target:.2f} USD")
                    print(f"   📈 Progreso  : {bar} {(pnl/target*100):.1f}%")
                    print(f"   ⏱  Tiempo    : {elapsed} | Muestras: {len(self._pnl_history)}")
                    print(f"\n   📉 GRAFICO PnL (desde apertura):")
                    print(self.pnl_chart())
                else:
                    bar = self.progress_bar(pnl, target)
                    print(f"[{ts}] {bar} ${pnl:+.4f} / ${target:.2f}", end="\r")

                if pnl >= target:
                    print(f"\n\n🎉 ¡OBJETIVO ALCANZADO! PnL: ${pnl:.4f} USD")
                    if CLOSE_ALL_ON_TARGET:
                        closed, results_for_tg = self.close_all(positions)
                        print(f"\n✅ {closed}/{len(positions)} posiciones cerradas")
                        print(f"💰 Ganancia: ~${pnl:.4f} USD\n")
                        if _TG_AVAILABLE:
                            tg.add_daily_pnl(pnl)
                            tg.set_last_cycle_pnl(pnl)
                            tg.notify_positions_closed(results_for_tg, pnl, self.cycle)
                    else:
                        print("🔔 Modo solo-avisar — posiciones NO cerradas")
                        if _TG_AVAILABLE:
                            tg.send(f"🔔 <b>OBJETIVO ALCANZADO (solo aviso)</b>\n"
                                    f"💰 PnL: ${pnl:+.4f} USD | Ciclo #{self.cycle}")
                    self._stop_chart_thread()
                    break

                time.sleep(CHECK_INTERVAL_SEC)

        except KeyboardInterrupt:
            self._stop_chart_thread()
            positions = self.get_open_positions()
            pnl = self.total_pnl(positions)
            print(f"\n\n⏹️  Monitor detenido")
            print(f"   PnL actual   : ${pnl:+.4f} USD")
            print(f"   Posiciones   : {len(positions)}")
            if _TG_AVAILABLE:
                tg.send(f"⏹️ <b>Monitor detenido manualmente</b>\n"
                        f"🕐 {datetime.now().strftime('%H:%M:%S')} | Ciclo #{self.cycle}\n"
                        f"💰 PnL actual: ${pnl:+.4f} USD | Posiciones: {len(positions)}")

        # Salida por stop externo (orquestador)
        if self._external_stop.is_set():
            self._stop_chart_thread()
            print(f"\n⏹️  [Monitor] Parada externa recibida — ciclo #{self.cycle} finalizado.")


if __name__ == "__main__":
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║      KRAKEN FUTURES PROFIT MONITOR                       ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    try:
        KrakenFuturesProfitMonitor().run()
    except Exception as e:
        print(f"\n❌ Error: {e}")
