"""
Kraken Futures Auto Trader
==========================
✅ Config hardcodeada (sin JSON externo)
✅ API nativa Kraken Futures
✅ Perpetuos PF_ multi-collateral (cuenta FLEX)
✅ Análisis técnico multi-timeframe (26 indicadores)
✅ Consenso 80% + confirmación 1D Strong
✅ SL/TP como órdenes separadas reduceOnly
✅ Cierre automático de posiciones > 72h
"""

import hashlib, hmac, base64, time, urllib.parse
import requests, pandas as pd, numpy as np
import pandas_ta as ta, warnings
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
warnings.filterwarnings("ignore")

try:
    import telegram_notifier as tg
    _TG_AVAILABLE = True
except ImportError:
    _TG_AVAILABLE = False
    print("⚠️  telegram_notifier.py no encontrado — notificaciones Telegram desactivadas")


# ╔══════════════════════════════════════════════════════════════╗
# ║                     CONFIGURACIÓN                            ║
# ╚══════════════════════════════════════════════════════════════╝

API_KEY    = "Qh9s+qkIjZXgLTTmSA6IcqgrpbZR/Ep7gqKVlYIiPAx7EC2iSLE5A5Hi"
API_SECRET = "aDe8x9pG+uQ/O2+izP0t6q7joPhPLlcxnTABYdo5tXs9B54k4m4/moLRyTDKVMGjBetcT0n1YTcXmJDQISWjeHLP"

# ── Símbolos ──────────────────────────────────────────────────
AUTO_LOAD_SYMBOLS = True          # True = autocarga todos los PF_ activos
SYMBOLS = [                       # Solo se usa si AUTO_LOAD_SYMBOLS = False
    "PF_XBTUSD",
    "PF_ETHUSD",
    "PF_SOLUSD",
    "PF_XRPUSD",
    "PF_ADAUSD",
]

# ── Análisis técnico ──────────────────────────────────────────
CONSENSUS_THRESHOLD = 0.8         # 0.8 = 80% de timeframes de acuerdo
REQUIRE_1D_STRONG   = True        # Exigir que 1D sea Strong Buy/Sell
REQUIRE_4H_STRONG   = True        # Exigir que 4H sea Strong Buy/Sell
REQUIRE_2H_STRONG   = True        # Exigir que 2H sea Strong Buy/Sell (si disponible)
REQUIRE_1H_STRONG   = True        # Exigir que 1H sea Strong Buy/Sell
REQUIRE_30M_STRONG  = True        # Exigir que 30M sea Strong Buy/Sell

# ── Gestión de riesgo ─────────────────────────────────────────
ATR_MULTIPLIER      = 1.0         # Distancia SL = ATR * este valor
RISK_REWARD_RATIO   = 1.0         # TP = SL * este valor
TARGET_NOTIONAL_USD = 270          # Nocional objetivo por trade en USD
MIN_CONTRACTS       = 1           # Mínimo de contratos a enviar

# ── Filtros de calidad de entrada (anti-SL inmediato) ────────
MIN_SL_PCT          = 0.4         # SL mínimo como % del precio (rechaza SLs demasiado ajustados)
MAX_SL_PCT          = 15.0        # SL máximo como % del precio (rechaza riesgo excesivo)
MAX_SPREAD_RATIO    = 0.25        # Spread máximo como fracción del SL (ej: 0.25 = spread ≤ 25% del SL)
MIN_ATR_RATIO       = 0.003       # ATR mínimo como fracción del precio (filtra mercados comprimidos)
SL_PROXIMITY_BUFFER = 1.5         # El SL no puede estar a menos de 1.5x el spread del precio actual

# ── Límites ───────────────────────────────────────────────────
MAX_POSITIONS       = 10           # Máximo posiciones simultáneas
MAX_DAILY_TRADES    = 10000          # Máximo trades por día

# ── Cierre automático por tiempo ──────────────────────────────
CLOSE_OLD_POSITIONS = True        # Activar cierre automático
MAX_ORDER_AGE_HOURS = 5          # Horas máximas de vida de una posición

# ── Horario de trading ───────────────────────────────────────
NO_TRADE_START_HOUR = 23          # Hora inicio de pausa nocturna (formato 24h)
NO_TRADE_END_HOUR   = 2           # Hora fin de pausa nocturna (formato 24h)


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

    # ── Público ──────────────────────────────────────────────
    def get_instruments(self):
        return requests.get(self.BASE_URL + "/derivatives/api/v3/instruments", timeout=15).json()

    def get_ohlc(self, symbol, resolution):
        # Endpoint correcto: /api/charts/v1/trade/{symbol}/{resolution}
        # resolution: "1h", "4h", "12h", "1d", "1w"
        url = f"{self.BASE_URL}/api/charts/v1/trade/{symbol}/{resolution}"
        return requests.get(url, timeout=15).json()

    def get_ticker(self, symbol):
        return requests.get(self.BASE_URL + f"/derivatives/api/v3/tickers/{symbol}", timeout=15).json()

    def get_tickers(self):
        return requests.get(self.BASE_URL + "/derivatives/api/v3/tickers", timeout=15).json()

    # ── Privado ───────────────────────────────────────────────
    def get_accounts(self):
        return self._request("GET", "/derivatives/api/v3/accounts")

    def get_open_orders(self):
        return self._request("GET", "/derivatives/api/v3/openorders")

    def get_open_positions(self):
        return self._request("GET", "/derivatives/api/v3/openpositions")

    def send_order(self, order_type, symbol, side, size,
                   limit_price=None, stop_price=None, reduce_only=False):
        p = {"orderType": order_type, "symbol": symbol, "side": side, "size": int(size)}
        if limit_price:  p["limitPrice"] = limit_price
        if stop_price:   p["stopPrice"]  = stop_price
        if reduce_only:  p["reduceOnly"] = "true"
        return self._request("POST", "/derivatives/api/v3/sendorder", p)

    def cancel_order(self, order_id):
        return self._request("POST", "/derivatives/api/v3/cancelorder", {"order_id": order_id})

    def cancel_all_orders(self, symbol=None):
        return self._request("POST", "/derivatives/api/v3/cancelallorders", {"symbol": symbol} if symbol else {})


# ╔══════════════════════════════════════════════════════════════╗
# ║                  ANALIZADOR TÉCNICO                          ║
# ╚══════════════════════════════════════════════════════════════╝

class FuturesTechnicalAnalyzer:

    TIMEFRAMES = {"30M": "30m", "1H": "1h", "2H": "2h", "4H": "4h", "12H": "12h", "1D": "1d", "1W": "1w"}

    def __init__(self, client: KrakenFuturesClient):
        self.client = client

    def get_all_symbols(self) -> List[str]:
        try:
            r = self.client.get_instruments()
            return sorted([
                i["symbol"] for i in r.get("instruments", [])
                if i.get("type") == "flexible_futures"
                and i.get("tradeable", False)
                and i["symbol"].startswith("PF_")
            ])
        except Exception as e:
            print(f"Error obteniendo símbolos: {e}"); return []

    def get_market_data(self, symbol: str, tf: str, bars: int = 500) -> Optional[pd.DataFrame]:
        try:
            res = self.TIMEFRAMES[tf]
            r   = self.client.get_ohlc(symbol, res)
            candles = r.get("candles", [])
            if not candles: return None
            df = pd.DataFrame(candles)
            df["time"] = pd.to_datetime(df["time"], unit="ms")
            df.set_index("time", inplace=True)
            df = df.rename(columns={"open":"Open","high":"High","low":"Low","close":"Close","volume":"Volume"})
            for c in ["Open","High","Low","Close","Volume"]:
                df[c] = df[c].astype(float)
            return df[["Open","High","Low","Close","Volume"]].tail(bars)
        except Exception:
            return None

    def calculate_indicators(self, df: pd.DataFrame) -> Optional[pd.DataFrame]:
        if df is None or len(df) < 200: return None
        try:
            df["RSI_14"]      = ta.rsi(df["Close"], length=14)
            stoch = ta.stoch(df["High"], df["Low"], df["Close"], k=14, d=3, smooth_k=3)
            if stoch is not None: df["Stoch_K"] = stoch["STOCHk_14_3_3"]
            df["CCI_20"]      = ta.cci(df["High"], df["Low"], df["Close"], length=20)
            adx = ta.adx(df["High"], df["Low"], df["Close"], length=14)
            if adx is not None:
                df["ADX_14"] = adx["ADX_14"]; df["DI_plus"] = adx["DMP_14"]; df["DI_minus"] = adx["DMN_14"]
            df["AO"]          = ta.ao(df["High"], df["Low"], fast=5, slow=34)
            df["Momentum_10"] = ta.mom(df["Close"], length=10)
            macd = ta.macd(df["Close"], fast=12, slow=26, signal=9)
            if macd is not None:
                df["MACD"] = macd["MACD_12_26_9"]; df["MACD_signal"] = macd["MACDs_12_26_9"]
            df["Williams_R"]  = ta.willr(df["High"], df["Low"], df["Close"], length=14)
            ema13 = ta.ema(df["Close"], length=13)
            df["BBP"]         = (df["High"] - ema13) + (df["Low"] - ema13)
            df["UO"]          = ta.uo(df["High"], df["Low"], df["Close"], fast=7, medium=14, slow=28)
            for p in [10, 20, 30, 50, 100, 200]:
                df[f"SMA_{p}"] = ta.sma(df["Close"], length=p)
                df[f"EMA_{p}"] = ta.ema(df["Close"], length=p)
            df["VWMA_20"] = ta.vwma(df["Close"], df["Volume"], length=20)
            df["HMA_9"]   = ta.hma(df["Close"], length=9)
            return df
        except Exception:
            return None

    def analyze_signal(self, df: pd.DataFrame) -> str:
        if df is None or len(df) < 200: return "Error"
        try:
            last = df.iloc[-1]; prev = df.iloc[-2]
            buy = sell = 0

            if pd.notna(last["RSI_14"]):
                if last["RSI_14"] < 30: buy += 1
                elif last["RSI_14"] > 70: sell += 1
            if pd.notna(last.get("Stoch_K")):
                if last["Stoch_K"] < 20: buy += 1
                elif last["Stoch_K"] > 80: sell += 1
            if pd.notna(last["CCI_20"]):
                if last["CCI_20"] < -100: buy += 1
                elif last["CCI_20"] > 100: sell += 1
            if pd.notna(last.get("ADX_14")) and last["ADX_14"] > 25:
                if last["DI_plus"] > last["DI_minus"]: buy += 1
                else: sell += 1
            if pd.notna(last["AO"]) and pd.notna(prev["AO"]):
                if last["AO"] > 0 and last["AO"] > prev["AO"]: buy += 1
                elif last["AO"] < 0 and last["AO"] < prev["AO"]: sell += 1
            if pd.notna(last["Momentum_10"]):
                if last["Momentum_10"] > 0: buy += 1
                elif last["Momentum_10"] < 0: sell += 1
            if pd.notna(last.get("MACD")) and pd.notna(last.get("MACD_signal")):
                if last["MACD"] > last["MACD_signal"]: buy += 1
                elif last["MACD"] < last["MACD_signal"]: sell += 1
            if pd.notna(last["Williams_R"]):
                if last["Williams_R"] < -80: buy += 1
                elif last["Williams_R"] > -20: sell += 1
            if pd.notna(last["BBP"]):
                if last["BBP"] > 0: buy += 1
                elif last["BBP"] < 0: sell += 1
            if pd.notna(last["UO"]):
                if last["UO"] < 30: buy += 1
                elif last["UO"] > 70: sell += 1
            for p in [10, 20, 30, 50, 100, 200]:
                if pd.notna(last[f"SMA_{p}"]):
                    if last["Close"] > last[f"SMA_{p}"]: buy += 1
                    else: sell += 1
                if pd.notna(last[f"EMA_{p}"]):
                    if last["Close"] > last[f"EMA_{p}"]: buy += 1
                    else: sell += 1
            if pd.notna(last["VWMA_20"]):
                if last["Close"] > last["VWMA_20"]: buy += 1
                else: sell += 1
            if pd.notna(last["HMA_9"]):
                if last["Close"] > last["HMA_9"]: buy += 1
                else: sell += 1

            total = buy + sell
            if total == 0: return "Neutral"
            br = buy / total; sr = sell / total
            if br >= 0.8: return "Strong Buy"
            if br >= 0.6: return "Buy"
            if sr >= 0.8: return "Strong Sell"
            if sr >= 0.6: return "Sell"
            return "Neutral"
        except Exception:
            return "Error"


# ╔══════════════════════════════════════════════════════════════╗
# ║                     AUTO TRADER                              ║
# ╚══════════════════════════════════════════════════════════════╝

class KrakenFuturesAutoTrader:

    def __init__(self, cycle: int = 1):
        self.client    = KrakenFuturesClient()
        self.analyzer  = FuturesTechnicalAnalyzer(self.client)
        self.trade_log = []
        self.cycle     = cycle

        result = self.client.get_accounts()
        if result.get("result") != "success":
            err = result.get("error")
            if _TG_AVAILABLE: tg.notify_error("KrakenFuturesAutoTrader.__init__", err, cycle)
            raise Exception(f"Auth fallida: {err}")

        flex = result["accounts"].get("flex", {})
        usd  = flex.get("currencies", {}).get("USD", {}).get("available", 0)
        self._balance_usd = usd
        print(f"✅ Conectado — Saldo FLEX: ${usd:.2f} USD")

    def get_open_orders(self) -> List[Dict]:
        try:
            orders = []
            for o in self.client.get_open_orders().get("openOrders", []):
                ts = o.get("receivedTime", 0)
                orders.append({
                    "id": o["order_id"], "symbol": o["symbol"],
                    "size": o["unfilledSize"],
                    "time": datetime.fromtimestamp(ts / 1000) if ts else datetime.now(),
                })
            return orders
        except Exception as e:
            print(f"Error órdenes: {e}"); return []

    def get_open_positions(self) -> List[Dict]:
        try:
            return [
                {"symbol": p["symbol"], "side": p["side"],
                 "size": p["size"], "fill_time": p.get("fillTime", "")}
                for p in self.client.get_open_positions().get("openPositions", [])
            ]
        except Exception as e:
            print(f"Error posiciones: {e}"); return []

    def close_old_orders_and_positions(self):
        if not CLOSE_OLD_POSITIONS: return
        max_age = timedelta(hours=MAX_ORDER_AGE_HOURS)
        now = datetime.utcnow(); closed = 0
        print(f"\n🔍 Barriendo operaciones antiguas (>{MAX_ORDER_AGE_HOURS}h)...")
        for o in self.get_open_orders():
            if now - o["time"] > max_age:
                r = self.client.cancel_order(o["id"])
                status = "✅" if r.get("result") == "success" else "❌"
                print(f"   {status} Orden {o['id']} ({o['symbol']}) cancelada"); closed += 1
        for p in self.get_open_positions():
            ft = p.get("fill_time", "")
            if ft:
                try:
                    if now - datetime.strptime(ft[:19], "%Y-%m-%dT%H:%M:%S") > max_age:
                        opp = "sell" if p["side"] == "long" else "buy"
                        r = self.client.send_order("mkt", p["symbol"], opp, p["size"], reduce_only=True)
                        status = "✅" if r.get("result") == "success" else "❌"
                        print(f"   {status} Posición {p['symbol']} cerrada"); closed += 1
                        if _TG_AVAILABLE and r.get("result") == "success":
                            age_h = (now - datetime.strptime(ft[:19], "%Y-%m-%dT%H:%M:%S")).total_seconds() / 3600
                            tg.notify_old_position_closed(p["symbol"], p["side"], age_h, 0.0)
                except Exception: pass
        print(f"   ✓ {closed} operaciones cerradas" if closed else "   ✓ Sin operaciones antiguas")

    def get_tradeable_symbols(self) -> List[str]:
        if AUTO_LOAD_SYMBOLS:
            print("\n🔍 Autocargando símbolos PF_...")
            syms = self.analyzer.get_all_symbols()
            if syms:
                print(f"   ✅ {len(syms)} perpetuos encontrados")
                return syms
        return SYMBOLS

    def analyze_all_timeframes(self, symbol: str) -> Dict[str, str]:
        results = {}
        for tf in ["30M", "1H", "2H", "4H", "12H", "1D", "1W"]:
            df = self.analyzer.get_market_data(symbol, tf)
            if df is not None and len(df) >= 200:
                df = self.analyzer.calculate_indicators(df)
                results[tf] = self.analyzer.analyze_signal(df)
            else:
                results[tf] = "Error"
        return results

    def check_consensus(self, signals: Dict) -> Tuple[str, bool]:
        valid = [s for s in signals.values() if s != "Error"]
        if not valid: return "Neutral", False
        total = len(valid)
        buys  = sum(1 for s in valid if "Buy"  in s)
        sells = sum(1 for s in valid if "Sell" in s)
        if buys  >= total * CONSENSUS_THRESHOLD:
            return ("Strong Buy" if sum(1 for s in valid if s == "Strong Buy") >= total * 0.5 else "Buy"), True
        if sells >= total * CONSENSUS_THRESHOLD:
            return ("Strong Sell" if sum(1 for s in valid if s == "Strong Sell") >= total * 0.5 else "Sell"), True
        return "Neutral", False

    def get_atr(self, symbol: str, tf: str, period: int = 14) -> Optional[float]:
        df = self.analyzer.get_market_data(symbol, tf, bars=period + 50)
        if df is None or len(df) < period: return None
        hl = df["High"] - df["Low"]
        hc = np.abs(df["High"] - df["Close"].shift())
        lc = np.abs(df["Low"]  - df["Close"].shift())
        return float(pd.concat([hl, hc, lc], axis=1).max(axis=1).rolling(period).mean().iloc[-1])

    def _get_spread(self, symbol: str) -> float:
        """Devuelve el spread bid-ask absoluto. 0.0 si no está disponible."""
        try:
            t = self.client.get_ticker(symbol)
            if t.get("result") != "success": return 0.0
            bid = float(t["ticker"].get("bid", 0) or 0)
            ask = float(t["ticker"].get("ask", 0) or 0)
            return (ask - bid) if bid > 0 and ask > 0 else 0.0
        except Exception:
            return 0.0

    def _check_entry_quality(self, symbol: str, price: float, atr: float, sl_pct: float, side: str) -> tuple:
        """
        Ejecuta todos los filtros anti-SL-inmediato.
        Devuelve (ok: bool, motivo: str)
        """
        # ── 1. SL demasiado ajustado ─────────────────────────────────
        if sl_pct < MIN_SL_PCT:
            return False, (f"SL muy ajustado ({sl_pct:.2f}% < mín {MIN_SL_PCT}%) — "
                           f"el ruido de mercado lo tocaría inmediatamente")

        # ── 2. SL demasiado amplio ────────────────────────────────────
        if sl_pct > MAX_SL_PCT:
            return False, (f"SL excesivo ({sl_pct:.2f}% > máx {MAX_SL_PCT}%) — "
                           f"riesgo por trade demasiado alto")

        # ── 3. ATR mínimo (mercado comprimido) ───────────────────────
        atr_ratio = atr / price
        if atr_ratio < MIN_ATR_RATIO:
            return False, (f"ATR relativo muy bajo ({atr_ratio*100:.3f}% < mín {MIN_ATR_RATIO*100:.3f}%) — "
                           f"mercado comprimido, ruptura impredecible")

        # ── 4. Spread vs SL ──────────────────────────────────────────
        spread = self._get_spread(symbol)
        if spread > 0:
            sl_distance = price * sl_pct / 100
            spread_ratio = spread / sl_distance
            if spread_ratio > MAX_SPREAD_RATIO:
                return False, (f"Spread demasiado alto vs SL "
                               f"(spread=${spread:.4f} = {spread_ratio*100:.1f}% del SL) — "
                               f"máx permitido: {MAX_SPREAD_RATIO*100:.0f}%")

            # ── 5. SL_PROXIMITY_BUFFER: el SL debe estar al menos N spreads del precio ──
            sl_price = price * (1 - sl_pct/100) if side == "buy" else price * (1 + sl_pct/100)
            gap_to_sl = abs(price - sl_price)
            if gap_to_sl < spread * SL_PROXIMITY_BUFFER:
                return False, (f"SL demasiado cerca del precio actual "
                               f"(gap=${gap_to_sl:.4f}, spread*{SL_PROXIMITY_BUFFER}=${spread*SL_PROXIMITY_BUFFER:.4f}) — "
                               f"el coste de entrada ya toca el SL")

        # ── 6. Proximidad a soporte/resistencia reciente (1H) ────────
        try:
            df = self.analyzer.get_market_data(symbol, "1H", bars=50)
            if df is not None and len(df) >= 20:
                recent = df.tail(20)
                sl_distance_abs = price * sl_pct / 100
                # Saltar el filtro si sl_distance_abs es cero o irrelevante (tokens de precio micro)
                if sl_distance_abs <= 0:
                    pass
                elif side == "buy":
                    recent_low = recent["Low"].min()
                    gap = price - recent_low
                    if gap < sl_distance_abs * 0.8:
                        # Formatear con suficientes cifras significativas
                        decimals = max(4, -int(np.floor(np.log10(abs(price)))) + 4) if price > 0 else 8
                        fmt = f"{{:.{decimals}f}}"
                        return False, (f"Precio demasiado cerca del mínimo reciente 1H "
                                       f"(mín={fmt.format(recent_low)}, gap={fmt.format(gap)} < "
                                       f"0.8x SL={fmt.format(sl_distance_abs)}) — SL quedaría bajo soporte reciente")
                else:
                    recent_high = recent["High"].max()
                    gap = recent_high - price
                    if gap < sl_distance_abs * 0.8:
                        decimals = max(4, -int(np.floor(np.log10(abs(price)))) + 4) if price > 0 else 8
                        fmt = f"{{:.{decimals}f}}"
                        return False, (f"Precio demasiado cerca del máximo reciente 1H "
                                       f"(máx={fmt.format(recent_high)}, gap={fmt.format(gap)} < "
                                       f"0.8x SL={fmt.format(sl_distance_abs)}) — SL quedaría sobre resistencia reciente")
        except Exception:
            pass  # Si falla el chequeo de proximidad, no bloqueamos la orden

        return True, "OK"

    def place_order(self, symbol: str, signal: str) -> bool:
        try:
            ticker = self.client.get_ticker(symbol)
            if ticker.get("result") != "success":
                print(f"   ⚠️  Sin precio para {symbol}"); return False
            price = float(ticker["ticker"].get("last") or ticker["ticker"].get("markPrice", 0))
            if not price: print(f"   ⚠️  Precio 0"); return False

            atr = self.get_atr(symbol, "1D")
            if not atr: print(f"   ⚠️  Sin ATR"); return False

            # Rechazar si 1 contrato (mínimo posible) cuesta más de 1.2x el objetivo
            min_order_cost = MIN_CONTRACTS * price
            if min_order_cost > TARGET_NOTIONAL_USD * 1.2:
                print(f"   ⛔ {symbol} omitido — 1 contrato = ${min_order_cost:,.2f} "
                      f"> 1.2x objetivo (${TARGET_NOTIONAL_USD * 1.2:,.2f})")
                return False

            # Calcular contratos para aproximar el nocional objetivo en USD
            raw_size = TARGET_NOTIONAL_USD / price
            size = max(MIN_CONTRACTS, round(raw_size))

            sl_pct = (atr * ATR_MULTIPLIER / price) * 100
            tp_pct = sl_pct * RISK_REWARD_RATIO
            side   = "buy" if "Buy" in signal else "sell"
            opp    = "sell" if side == "buy" else "buy"
            sl     = round(price * (1 - sl_pct/100 if side == "buy" else 1 + sl_pct/100), 2)
            tp     = round(price * (1 + tp_pct/100 if side == "buy" else 1 - tp_pct/100), 2)

            # ── Filtros de calidad de entrada ────────────────────────
            quality_ok, quality_reason = self._check_entry_quality(symbol, price, atr, sl_pct, side)
            if not quality_ok:
                print(f"   🚫 {symbol} rechazado — {quality_reason}")
                return False
            # ────────────────────────────────────────────────────────

            r = self.client.send_order("mkt", symbol, side, size)
            if r.get("result") != "success":
                print(f"   ❌ {r}"); return False

            oid = r.get("sendStatus", {}).get("order_id", "N/A")
            notional = size * price
            print(f"\n   🎯 {symbol} | {side.upper()} {size} contratos | ${price:,.4f} | ~${notional:,.2f} nocional | ID:{oid}")

            sl_r = self.client.send_order("stp", symbol, opp, size, stop_price=sl, reduce_only=True)
            tp_r = self.client.send_order("take_profit", symbol, opp, size, stop_price=tp, reduce_only=True)
            print(f"      SL ${sl:,.4f} ({sl_pct:.1f}%) {'✅' if sl_r.get('result')=='success' else '⚠️'} | "
                  f"TP ${tp:,.4f} ({tp_pct:.1f}%) {'✅' if tp_r.get('result')=='success' else '⚠️'}")

            if _TG_AVAILABLE:
                tg.notify_order_opened(symbol, side, size, price, sl, tp, round(notional, 2), self.cycle)

            self.trade_log.append({"timestamp": datetime.now().isoformat(), "symbol": symbol,
                                   "side": side, "size": size, "price": price, "notional_usd": round(notional, 2),
                                   "sl": sl, "tp": tp, "order_id": oid})
            return True
        except Exception as e:
            print(f"   ❌ Error: {e}"); return False

    def get_margin_level(self) -> Optional[float]:
        """Devuelve el nivel de margen en % (portfolioValue / initialMargin * 100).
        Retorna None si no hay margen usado (sin posiciones abiertas → margen ilimitado)."""
        try:
            result = self.client.get_accounts()
            if result.get("result") != "success":
                return None
            flex = result["accounts"].get("flex", {})
            portfolio_value = float(flex.get("portfolioValue", 0))
            initial_margin  = float(flex.get("initialMargin", 0))
            if initial_margin <= 0:
                return None  # Sin margen usado, no hay restricción
            return (portfolio_value / initial_margin) * 100
        except Exception as e:
            print(f"   ⚠️  Error obteniendo margen: {e}")
            return None

    def can_trade(self) -> bool:
        if len(self.get_open_positions()) >= MAX_POSITIONS:
            print(f"   ⚠️  Límite posiciones ({MAX_POSITIONS})"); return False
        today = datetime.now().strftime("%Y-%m-%d")
        if sum(1 for t in self.trade_log if t["timestamp"][:10] == today) >= MAX_DAILY_TRADES:
            print(f"   ⚠️  Límite diario ({MAX_DAILY_TRADES})"); return False
        return True

    def run(self):
        print("\n" + "="*65)
        print("🤖 KRAKEN FUTURES AUTO TRADER")
        print("="*65)
        print(f"Fecha: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Consenso: {int(CONSENSUS_THRESHOLD*100)}% | "
              f"1D: {'✅' if REQUIRE_1D_STRONG else '—'} "
              f"4H: {'✅' if REQUIRE_4H_STRONG else '—'} "
              f"2H: {'✅' if REQUIRE_2H_STRONG else '—'} "
              f"1H: {'✅' if REQUIRE_1H_STRONG else '—'} "
              f"30M: {'✅' if REQUIRE_30M_STRONG else '—'} Strong requerido")
        print(f"ATR x{ATR_MULTIPLIER} | RR 1:{RISK_REWARD_RATIO} | Nocional objetivo: ~${TARGET_NOTIONAL_USD} USD")
        print("="*65)

        # ── Ventana horaria: no operar entre NO_TRADE_START_HOUR y NO_TRADE_END_HOUR
        current_hour = datetime.now().hour
        in_blackout  = (
            current_hour >= NO_TRADE_START_HOUR or current_hour < NO_TRADE_END_HOUR
        )
        if in_blackout:
            print(f"\n🌙 PAUSA NOCTURNA activa ({NO_TRADE_START_HOUR:02d}:00 – {NO_TRADE_END_HOUR:02d}:00) "
                  f"— son las {datetime.now().strftime('%H:%M')}. Sin nuevas órdenes.")
            if _TG_AVAILABLE:
                tg.send(f"\U0001f319 <b>PAUSA NOCTURNA</b>\n"
                        f"\U0001f550 {datetime.now().strftime('%H:%M')} — "
                        f"Sin trading entre las {NO_TRADE_START_HOUR:02d}:00 y las {NO_TRADE_END_HOUR:02d}:00")
            return

        self.close_old_orders_and_positions()
        symbols = self.get_tradeable_symbols()
        if not symbols: print("❌ Sin símbolos"); return

        EMOJI = {"Strong Buy":"🟢","Buy":"🟩","Neutral":"⚪","Sell":"🟥","Strong Sell":"🔴","Error":"❌"}
        trades = 0

        for i, sym in enumerate(symbols, 1):

            # ── Guardianes rápidos ANTES de analizar el símbolo ──────────
            open_pos = self.get_open_positions()
            if len(open_pos) >= MAX_POSITIONS:
                print(f"\n🛑 Límite de {MAX_POSITIONS} posiciones alcanzado — deteniendo búsqueda.")
                break

            margin = self.get_margin_level()
            if margin is not None and margin < 100:
                print(f"\n🛑 Nivel de margen crítico ({margin:.1f}% < 100%) — deteniendo búsqueda.")
                if _TG_AVAILABLE: tg.notify_margin_warning(margin, self.cycle)
                break

            today = datetime.now().strftime("%Y-%m-%d")
            if sum(1 for t in self.trade_log if t["timestamp"][:10] == today) >= MAX_DAILY_TRADES:
                print(f"\n🛑 Límite diario de {MAX_DAILY_TRADES} trades alcanzado — deteniendo búsqueda.")
                break
            # ─────────────────────────────────────────────────────────────

            margin_str = f"  |  Margen: {margin:.1f}%" if margin is not None else ""
            print(f"\n[{i}/{len(symbols)}] 📊 {sym}{margin_str}")
            signals = self.analyze_all_timeframes(sym)
            print("  " + " ".join(f"{tf}:{EMOJI.get(s,'?')}" for tf, s in signals.items()))

            consensus, ok = self.check_consensus(signals)
            if not ok: print("  ⚪ Sin consenso"); continue
            print(f"  ✅ {consensus}")

            if REQUIRE_1D_STRONG and signals.get("1D") not in ["Strong Buy","Strong Sell"]:
                print(f"  ⚠️  1D no STRONG ({signals.get('1D')})"); continue

            if REQUIRE_4H_STRONG and signals.get("4H") not in ["Strong Buy","Strong Sell"]:
                print(f"  ⚠️  4H no STRONG ({signals.get('4H')})"); continue

            if REQUIRE_2H_STRONG and "2H" in signals and signals.get("2H") not in ["Strong Buy","Strong Sell","Error"]:
                print(f"  ⚠️  2H no STRONG ({signals.get('2H')})"); continue

            if REQUIRE_1H_STRONG and signals.get("1H") not in ["Strong Buy","Strong Sell"]:
                print(f"  ⚠️  1H no STRONG ({signals.get('1H')})"); continue

            if REQUIRE_30M_STRONG and signals.get("30M") not in ["Strong Buy","Strong Sell","Error"]:
                print(f"  ⚠️  30M no STRONG ({signals.get('30M')})"); continue

            if self.place_order(sym, consensus):
                trades += 1; time.sleep(1)

        print("\n" + "="*65)
        print(f"📊 FIN — Trades: {trades} | Posiciones: {len(self.get_open_positions())}")
        print("="*65)

        if _TG_AVAILABLE:
            tg.notify_orders_summary(self.trade_log, self.cycle)

        if self.trade_log:
            fname = f"futures_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
            pd.DataFrame(self.trade_log).to_csv(fname, index=False)
            print(f"✓ Log: {fname}")


if __name__ == "__main__":
    print("""
    ╔══════════════════════════════════════════════════════════╗
    ║         KRAKEN FUTURES AUTO TRADER                       ║
    ╚══════════════════════════════════════════════════════════╝
    """)
    try:
        KrakenFuturesAutoTrader().run()
    except Exception as e:
        print(f"\n❌ Error: {e}")