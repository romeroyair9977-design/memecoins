"""
Escáner de memecoins en Solana (para correr en GitHub Actions).

Uso:
  python scanner.py run            # escanea + actualiza seguimiento + avisa por Telegram (si está configurado)
  python scanner.py check <MINT>   # revisa UN token con todos los filtros y muestra el detalle

Qué hace:
  1. Junta tokens de DexScreener (perfiles/boosts) y GeckoTerminal (pools nuevos y en tendencia).
  2. Filtros de mercado: liquidez, volumen, actividad, compras vs ventas, edad, FDV.
  3. Dos tipos de candidato:
       - "nuevo":     token joven (hasta MAX_EDAD_H horas)
       - "despertar": token más viejo cuyo volumen se disparó de golpe (estilo AGENC / Dacity)
  4. Filtros de seguridad con RugCheck: mint/freeze autoridad, rugged, riesgos "danger",
     concentración del top 10 de holders.
  5. Guarda los que pasan en data/candidatos.csv y sigue su precio 48 h en data/seguimiento.csv.
  6. Si configuras Telegram, te manda los nombres al celular.
  7. Paper trading: simula compras/ventas con reglas fijas (TP, SL, tiempo máx.) en data/paper_*.csv.

IMPORTANTE: esto NO predice cuál sube. Descarta lo peligroso y marca señales de momentum.
"""

import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

import requests

# ======================= CONFIGURACIÓN (ajusta a tu gusto) =======================
# --- mercado ---
MIN_LIQ_USD = 15_000          # liquidez mínima del pool
MIN_VOL_H24 = 20_000          # volumen mínimo en 24 h
MIN_TXNS_H1 = 30              # compras + ventas mínimas en la última hora
MIN_RATIO_COMPRAS = 0.40      # compras / (compras + ventas) en 1 h
MIN_EDAD_MIN = 30             # no entrar a tokens de menos de X minutos
MAX_FDV_USD = 20_000_000      # capitalización máxima
MIN_LIQ_SOBRE_FDV = 0.02      # liquidez / FDV mínima (si es muy baja, es fácil manipular)

# --- tipo "nuevo" ---
MAX_EDAD_H = 72               # "nuevo" = hasta X horas de creado

# --- tipo "despertar" (token viejo que de repente tiene mucho volumen) ---
ACTIVAR_DESPERTAR = True
DESPERTAR_ACEL = 4.0          # volumen de la última hora >= 4x el promedio horario del resto del día
DESPERTAR_VOL_H1 = 8_000      # y al menos X USD de volumen en la última hora

# --- seguridad (RugCheck) ---
EXIGIR_RUGCHECK = True        # si RugCheck no responde, el token NO pasa
EXIGIR_SIN_MINT_FREEZE = True # rechaza si aún pueden crear tokens o congelar cuentas
MAX_TOP10_PCT = 80            # máx % del supply en los 10 mayores holders (aprox: puede incluir el pool)
MIN_HOLDERS = 150             # holders mínimos (0 para desactivar)

# --- seguimiento y avisos ---
HORAS_SEGUIMIENTO = 48
HORAS_SIN_REPETIR = 24        # no volver a avisar del mismo token antes de X horas
MAX_REVISAR_POR_CORRIDA = 150 # tope de tokens a consultar por ejecución
MAX_AVISOS_POR_CORRIDA = 10

# --- PAPER TRADING (simulación, sin plata real) ---
# Cada estrategia es una cuenta simulada aparte que opera los MISMOS candidatos,
# para comparar cuál regla de salida rinde mejor.
PAPER_ACTIVO = True
PAPER_CAPITAL = 10.0               # capital simulado inicial POR estrategia (USD)
PAPER_MONTO = 2.0                  # USD por operación
PAPER_SLIPPAGE_ENTRADA_PCT = 1.0   # deslizamiento simulado al comprar
PAPER_SLIPPAGE_SALIDA_PCT = 1.0    # deslizamiento simulado al vender
PAPER_TIPOS = ("nuevo", "despertar")  # qué tipos de candidato se operan
PAPER_MAX_ABIERTAS = 5
PAPER_ESTRATEGIAS = {
    # Solo queda la regla A: en la prueba, B (escalonado) y C (parcial) salieron claramente peores
    # en memecoins. Puedes volver a añadir otras copiando el formato.
    # tp: take profit | sl: stop loss | trail_*: stop escalonado | parcial_*: venta parcial
    "A_fija": {"tp": 60, "sl": 35, "trail_activar": None, "trail_dist": None,
               "parcial_en": None, "parcial_frac": 0.0, "max_horas": 4},
}

# --- VIGILANCIA DE POSICIONES ABIERTAS ---
# Antes el precio se miraba solo cada 30 min y los stops salían muy tarde (stop de -35% cerraba en -63%).
# Ahora, tras cada escaneo, el bot se queda vigilando las posiciones abiertas cada VIGILAR_CADA_SEG segundos
# durante hasta VIGILAR_MIN minutos (termina antes si no quedan posiciones abiertas).
VIGILAR_MIN = 25
VIGILAR_CADA_SEG = 45

# --- FILTROS NUEVOS (hipótesis a probar, NO garantías) ---
MAX_CAMBIO_H1_PCT = 150    # no entrar si ya subió más de X% en la última hora (perseguir un pico)
MIN_CAMBIO_H1_PCT = -20    # no entrar si ya cae más de X% en la última hora
BLOQUEAR_SIMBOLOS_REPETIDOS = True  # evita copias: mismo símbolo con otro contrato que uno ya visto
# ================================================================================

DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
RUG = "https://api.rugcheck.xyz/v1"
DATA = "data"
CAND = os.path.join(DATA, "candidatos.csv")
SEG = os.path.join(DATA, "seguimiento.csv")
PAPER_STATE = os.path.join(DATA, "paper_estado.json")
PAPER_TRADES = os.path.join(DATA, "paper_trades.csv")
PAPER_BALANCE = os.path.join(DATA, "paper_balance.csv")

CAND_COLS = [
    "fecha_utc", "mint", "simbolo", "nombre", "tipo", "precio_usd", "liquidez_usd",
    "vol_h24", "vol_h1", "aceleracion_vol", "cambio_h1_pct", "cambio_h24_pct", "fdv",
    "edad_h", "compras_h1", "ventas_h1", "holders", "top10_pct", "avisos_rugcheck",
]
SEG_COLS = [
    "fecha_utc", "mint", "simbolo", "tipo", "precio_entrada", "precio_actual",
    "cambio_pct", "liquidez_usd",
]

PAPER_COLS = [
    "estrategia", "fecha_entrada", "fecha_salida", "mint", "simbolo", "tipo", "precio_entrada",
    "precio_salida", "monto_usd", "resultado_usd", "resultado_pct", "motivo_salida", "horas",
    "liquidez_usd", "vol_h1", "aceleracion_vol", "cambio_h1_pct", "edad_h", "holders", "top10_pct",
]
BAL_COLS = ["fecha_utc", "estrategia", "caja", "valor_abiertas", "total", "abiertas", "cerradas", "ganadas"]

HEADERS = {"User-Agent": "memecoin-scanner/2.0", "Accept": "application/json"}


def ahora():
    return datetime.now(timezone.utc)


def num(x, d=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return d


def get_json(url, intentos=3, headers=None):
    for i in range(intentos):
        try:
            r = requests.get(url, headers=headers or HEADERS, timeout=20)
            if r.status_code == 429:
                time.sleep(5 * (i + 1))
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            if i == intentos - 1:
                print(f"  ! fallo {url}: {e}")
                return None
            time.sleep(2)
    return None


# ----------------------------- CSV -----------------------------
def leer_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def añadir_csv(path, cols, filas):
    if not filas:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            cabecera = f.readline().strip().split(",")
        if cabecera != cols:  # formato viejo: lo apartamos para no mezclar columnas
            os.replace(path, path + ".viejo")
    nuevo = not os.path.exists(path)
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        if nuevo:
            w.writeheader()
        w.writerows(filas)


# ----------------------------- FUENTES -----------------------------
def mints_recientes():
    """Mints de Solana desde DexScreener y GeckoTerminal."""
    mints = []
    for ruta in ("/token-profiles/latest/v1", "/token-boosts/latest/v1", "/token-boosts/top/v1"):
        data = get_json(DS + ruta)
        if isinstance(data, list):
            for it in data:
                if it.get("chainId") == "solana" and it.get("tokenAddress"):
                    mints.append(it["tokenAddress"])
    for ruta in ("/new_pools", "/trending_pools"):
        data = get_json(GT + ruta, headers={"Accept": "application/json;version=20230302"})
        if isinstance(data, dict):
            for pool in data.get("data", []):
                tid = (((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}).get("id", "")
                if tid.startswith("solana_"):
                    mints.append(tid.split("_", 1)[1])
        time.sleep(1)
    return list(dict.fromkeys(mints))


def pares_de(mints):
    """Devuelve {mint: mejor_par_por_liquidez} consultando de a 30."""
    mejores = {}
    for i in range(0, len(mints), 30):
        grupo = mints[i:i + 30]
        data = get_json(f"{DS}/tokens/v1/solana/{','.join(grupo)}")
        if not isinstance(data, list):
            continue
        for p in data:
            mint = (p.get("baseToken") or {}).get("address")
            if not mint:
                continue
            liq = (p.get("liquidity") or {}).get("usd") or 0
            actual = mejores.get(mint)
            if actual is None or liq > ((actual.get("liquidity") or {}).get("usd") or 0):
                mejores[mint] = p
        time.sleep(0.3)
    return mejores


# ----------------------------- FILTROS -----------------------------
def metricas(p):
    liq = num((p.get("liquidity") or {}).get("usd"))
    vol = p.get("volume") or {}
    vol24, vol1 = num(vol.get("h24")), num(vol.get("h1"))
    fdv = num(p.get("fdv") or p.get("marketCap"))
    tx = (p.get("txns") or {}).get("h1") or {}
    compras, ventas = int(tx.get("buys", 0) or 0), int(tx.get("sells", 0) or 0)
    creado = p.get("pairCreatedAt")
    edad_h = (ahora().timestamp() * 1000 - creado) / 3_600_000 if creado else None
    resto = (vol24 - vol1) / 23 if vol24 > vol1 else 0
    acel = vol1 / resto if resto > 0 else None
    pc = p.get("priceChange") or {}
    return {
        "liq": liq, "vol24": vol24, "vol1": vol1, "fdv": fdv,
        "compras": compras, "ventas": ventas, "edad_h": edad_h, "acel": acel,
        "cambio_h1": num(pc.get("h1")), "cambio_h24": num(pc.get("h24")),
    }


def evaluar_mercado(m):
    """Devuelve (motivos_de_rechazo, tipo)."""
    motivos, tipo = [], None
    if m["liq"] < MIN_LIQ_USD:
        motivos.append(f"liquidez {m['liq']:,.0f} < {MIN_LIQ_USD:,}")
    if m["vol24"] < MIN_VOL_H24:
        motivos.append(f"vol24h {m['vol24']:,.0f} < {MIN_VOL_H24:,}")
    n = m["compras"] + m["ventas"]
    if n < MIN_TXNS_H1:
        motivos.append(f"pocas operaciones en 1h ({n})")
    elif m["compras"] / n < MIN_RATIO_COMPRAS:
        motivos.append("muchas más ventas que compras en 1h")
    if m["cambio_h1"] > MAX_CAMBIO_H1_PCT:
        motivos.append(f"ya subió {m['cambio_h1']:.0f}% en 1h (perseguiría el pico)")
    if m["cambio_h1"] < MIN_CAMBIO_H1_PCT:
        motivos.append(f"cae {m['cambio_h1']:.0f}% en 1h")
    if m["fdv"] and m["fdv"] > MAX_FDV_USD:
        motivos.append(f"FDV {m['fdv']:,.0f} > {MAX_FDV_USD:,}")
    if m["fdv"] and m["liq"] / m["fdv"] < MIN_LIQ_SOBRE_FDV:
        motivos.append("liquidez muy baja frente al FDV")

    e = m["edad_h"]
    if e is None:
        motivos.append("sin fecha de creación")
    elif e * 60 < MIN_EDAD_MIN:
        motivos.append(f"muy nuevo ({e * 60:.0f} min)")
    elif e <= MAX_EDAD_H:
        tipo = "nuevo"
    elif ACTIVAR_DESPERTAR and m["acel"] and m["acel"] >= DESPERTAR_ACEL \
            and m["vol1"] >= DESPERTAR_VOL_H1 and m["cambio_h1"] >= 0:
        tipo = "despertar"
    else:
        motivos.append(f"viejo ({e:.0f} h) y sin pico de volumen")
    return motivos, tipo


def evaluar_rugcheck(mint):
    """Devuelve (ok, avisos, holders, top10_pct)."""
    data = get_json(f"{RUG}/tokens/{mint}/report")
    if not isinstance(data, dict):
        return (not EXIGIR_RUGCHECK), ["rugcheck sin datos"], None, None

    problemas, avisos = [], []
    if data.get("rugged"):
        problemas.append("marcado como RUGGED")
    if EXIGIR_SIN_MINT_FREEZE:
        if data.get("mintAuthority"):
            problemas.append("mint authority activa (pueden crear más tokens)")
        if data.get("freezeAuthority"):
            problemas.append("freeze authority activa (pueden congelar cuentas)")
    for r in data.get("risks") or []:
        if r.get("level") == "danger":
            problemas.append(f"danger: {r.get('name', '?')}")
        elif r.get("level") == "warn":
            avisos.append(r.get("name", "?"))

    holders = data.get("totalHolders")
    top = data.get("topHolders") or []
    top10 = round(sum(num(h.get("pct")) for h in top[:10]), 1) if top else None
    if top10 is not None and top10 > MAX_TOP10_PCT:
        problemas.append(f"top10 holders {top10}% > {MAX_TOP10_PCT}%")
    if MIN_HOLDERS and holders is not None and holders < MIN_HOLDERS:
        problemas.append(f"pocos holders ({holders})")

    texto = [f"X {p}" for p in problemas] + [f"aviso: {a}" for a in avisos]
    return (len(problemas) == 0), texto, holders, top10


# ----------------------------- SALIDAS -----------------------------
def fila_candidato(p, m, tipo, avisos, holders, top10):
    return {
        "fecha_utc": ahora().strftime("%Y-%m-%d %H:%M"),
        "mint": p["baseToken"]["address"],
        "simbolo": p["baseToken"].get("symbol", ""),
        "nombre": p["baseToken"].get("name", ""),
        "tipo": tipo,
        "precio_usd": p.get("priceUsd", ""),
        "liquidez_usd": round(m["liq"]),
        "vol_h24": round(m["vol24"]),
        "vol_h1": round(m["vol1"]),
        "aceleracion_vol": round(m["acel"], 1) if m["acel"] else "",
        "cambio_h1_pct": m["cambio_h1"],
        "cambio_h24_pct": m["cambio_h24"],
        "fdv": round(m["fdv"]),
        "edad_h": round(m["edad_h"], 1) if m["edad_h"] is not None else "",
        "compras_h1": m["compras"],
        "ventas_h1": m["ventas"],
        "holders": holders if holders is not None else "",
        "top10_pct": top10 if top10 is not None else "",
        "avisos_rugcheck": " | ".join(avisos),
    }


def avisar_telegram(filas):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat or not filas:
        return
    for f in filas[:MAX_AVISOS_POR_CORRIDA]:
        txt = (
            f"{'🆕' if f['tipo'] == 'nuevo' else '⚡'} {f['simbolo']} ({f['tipo']})\n"
            f"Cap: ${f['fdv']:,}  Liq: ${f['liquidez_usd']:,}\n"
            f"Vol 1h: ${f['vol_h1']:,}  Aceleración: {f['aceleracion_vol'] or '-'}x\n"
            f"Cambio 1h: {f['cambio_h1_pct']}%  24h: {f['cambio_h24_pct']}%\n"
            f"Holders: {f['holders'] or '?'}  Top10: {f['top10_pct'] or '?'}%\n"
            f"https://dexscreener.com/solana/{f['mint']}\n{f['mint']}\n"
            f"(Pasó filtros de seguridad. No es recomendación de compra.)"
        )
        try:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": txt, "disable_web_page_preview": True},
                timeout=15,
            )
        except Exception as e:  # noqa: BLE001
            print(f"  ! Telegram falló: {e}")
        time.sleep(0.5)


# ----------------------------- PAPER TRADING -----------------------------
def paper_cargar():
    est = None
    if os.path.exists(PAPER_STATE):
        with open(PAPER_STATE, encoding="utf-8") as f:
            est = json.load(f)
    if not est or "estrategias" not in est:  # primera vez o formato viejo
        est = {"estrategias": {}}
    for nombre in PAPER_ESTRATEGIAS:
        est["estrategias"].setdefault(
            nombre, {"caja": PAPER_CAPITAL, "abiertas": [], "cerradas": 0, "ganadas": 0})
    return est


def paper_guardar(est):
    os.makedirs(DATA, exist_ok=True)
    with open(PAPER_STATE, "w", encoding="utf-8") as f:
        json.dump(est, f, indent=2)


def paper_trading(nuevos, registrar_balance=True):
    """Simula varias reglas de salida en cuentas separadas, sobre los mismos candidatos."""
    if not PAPER_ACTIVO:
        return
    est = paper_cargar()
    t = ahora()
    s_out = 1 - PAPER_SLIPPAGE_SALIDA_PCT / 100

    # precios actuales de todas las posiciones abiertas (una sola consulta)
    mints = sorted({p["mint"] for c in est["estrategias"].values() for p in c["abiertas"]})
    precios = {}
    if mints:
        pares = pares_de(mints)
        precios = {m: num(p.get("priceUsd")) for m, p in pares.items()}

    filas_cerradas, filas_bal = [], []
    for nombre, cfg in PAPER_ESTRATEGIAS.items():
        cta = est["estrategias"][nombre]

        # 1) revisar posiciones abiertas
        restantes = []
        for pos in cta["abiertas"]:
            horas = (t - datetime.fromisoformat(pos["fecha_entrada"])).total_seconds() / 3600
            precio = precios.get(pos["mint"])
            motivo, salida = None, None
            if precio:
                pos["max_precio"] = max(pos.get("max_precio", pos["precio_entrada"]), precio)
                cambio = precio / pos["precio_entrada"] - 1
                max_cambio = pos["max_precio"] / pos["precio_entrada"] - 1

                # toma parcial de ganancias (una sola vez)
                if cfg["parcial_en"] and not pos.get("parcial") and cambio >= cfg["parcial_en"] / 100:
                    vender = pos["tokens"] * cfg["parcial_frac"]
                    valor = vender * precio * s_out
                    pos["tokens"] -= vender
                    pos["recuperado"] = pos.get("recuperado", 0.0) + valor
                    pos["parcial"] = True
                    cta["caja"] += valor
                    print(f"  PAPER[{nombre}] parcial {pos['simbolo']}: vende {cfg['parcial_frac']:.0%} a {precio:.8g}")

                if cfg["tp"] and cambio >= cfg["tp"] / 100:
                    motivo = "take_profit"
                elif (cfg["trail_activar"] is not None and max_cambio >= cfg["trail_activar"] / 100
                      and precio <= pos["max_precio"] * (1 - cfg["trail_dist"] / 100)):
                    motivo = "stop_escalonado"
                elif cambio <= -cfg["sl"] / 100:
                    motivo = "stop_loss"
                elif horas >= cfg["max_horas"]:
                    motivo = "tiempo"
                if motivo:
                    salida = precio * s_out
            elif horas >= cfg["max_horas"]:
                motivo, salida = "sin_datos_perdida_total", 0.0  # el token desapareció

            if not motivo:
                restantes.append(pos)
                continue
            valor = pos["tokens"] * salida
            cta["caja"] += valor
            resultado = pos.get("recuperado", 0.0) + valor - pos["monto"]
            cta["cerradas"] += 1
            cta["ganadas"] += 1 if resultado > 0 else 0
            filas_cerradas.append({
                "estrategia": nombre, "fecha_entrada": pos["fecha_entrada"],
                "fecha_salida": t.isoformat(timespec="minutes"), "mint": pos["mint"],
                "simbolo": pos["simbolo"], "tipo": pos["tipo"],
                "precio_entrada": pos["precio_entrada"], "precio_salida": salida,
                "monto_usd": pos["monto"], "resultado_usd": round(resultado, 4),
                "resultado_pct": round(resultado / pos["monto"] * 100, 1),
                "motivo_salida": motivo, "horas": round(horas, 2),
                **{k_: pos.get("feat", {}).get(k_, "") for k_ in
                   ("liquidez_usd", "vol_h1", "aceleracion_vol", "cambio_h1_pct", "edad_h", "holders", "top10_pct")},
            })
            print(f"  PAPER[{nombre}] cierre {pos['simbolo']}: {motivo} {resultado:+.2f} USD")
        cta["abiertas"] = restantes

        # 2) abrir posiciones nuevas
        ya = {p["mint"] for p in cta["abiertas"]}
        for c in nuevos or []:
            if c["tipo"] not in PAPER_TIPOS or c["mint"] in ya:
                continue
            if cta["caja"] < PAPER_MONTO or len(cta["abiertas"]) >= PAPER_MAX_ABIERTAS:
                break
            base = num(c["precio_usd"])
            if not base:
                continue
            entrada = base * (1 + PAPER_SLIPPAGE_ENTRADA_PCT / 100)
            cta["caja"] -= PAPER_MONTO
            cta["abiertas"].append({
                "mint": c["mint"], "simbolo": c["simbolo"], "tipo": c["tipo"],
                "fecha_entrada": t.isoformat(timespec="minutes"),
                "precio_entrada": entrada, "monto": PAPER_MONTO,
                "tokens": PAPER_MONTO / entrada, "max_precio": entrada,
                "feat": {k_: c.get(k_, "") for k_ in
                         ("liquidez_usd", "vol_h1", "aceleracion_vol", "cambio_h1_pct", "edad_h", "holders", "top10_pct")},
            })
            ya.add(c["mint"])
            print(f"  PAPER[{nombre}] compra {c['simbolo']} a {entrada:.8g}")

        # 3) balance de esta estrategia
        valor_ab = sum(p["tokens"] * (precios.get(p["mint"]) or p["precio_entrada"]) for p in cta["abiertas"])
        total = cta["caja"] + valor_ab
        filas_bal.append({
            "fecha_utc": t.strftime("%Y-%m-%d %H:%M"), "estrategia": nombre,
            "caja": round(cta["caja"], 4), "valor_abiertas": round(valor_ab, 4),
            "total": round(total, 4), "abiertas": len(cta["abiertas"]),
            "cerradas": cta["cerradas"], "ganadas": cta["ganadas"],
        })
        if registrar_balance:
            print(f"PAPER[{nombre}] total {total:.2f} USD | caja {cta['caja']:.2f} | "
                  f"abiertas {len(cta['abiertas'])} | cerradas {cta['cerradas']} | ganadas {cta['ganadas']}")

    añadir_csv(PAPER_TRADES, PAPER_COLS, filas_cerradas)
    if registrar_balance:
        añadir_csv(PAPER_BALANCE, BAL_COLS, filas_bal)
    paper_guardar(est)


def paper_vigilar():
    """Tras el escaneo, vigila las posiciones abiertas cada pocos segundos (stops más realistas)."""
    if not PAPER_ACTIVO:
        return
    ciclos = int(VIGILAR_MIN * 60 // VIGILAR_CADA_SEG)
    for n in range(ciclos):
        est = paper_cargar()
        if not any(c["abiertas"] for c in est["estrategias"].values()):
            print("Vigilancia: no quedan posiciones abiertas, termino.")
            break
        time.sleep(VIGILAR_CADA_SEG)
        paper_trading([], registrar_balance=False)
    paper_trading([], registrar_balance=True)


# ----------------------------- FLUJOS -----------------------------
def vistos_recientes():
    limite = ahora().timestamp() - HORAS_SIN_REPETIR * 3600
    vistos = set()
    for r in leer_csv(CAND):
        try:
            t = datetime.strptime(r["fecha_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        if t.timestamp() >= limite:
            vistos.add(r["mint"])
    return vistos


def escanear():
    vistos = vistos_recientes()
    mints = [m for m in mints_recientes() if m not in vistos][:MAX_REVISAR_POR_CORRIDA]
    print(f"Tokens por revisar: {len(mints)}")
    pares = pares_de(mints)

    # símbolos ya vistos en las últimas horas (otro contrato con el mismo símbolo suele ser una copia)
    simbolos_vistos = set()
    if BLOQUEAR_SIMBOLOS_REPETIDOS:
        limite_ = ahora().timestamp() - HORAS_SIN_REPETIR * 3600
        for r_ in leer_csv(CAND):
            try:
                t_ = datetime.strptime(r_["fecha_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
            except (ValueError, KeyError):
                continue
            if t_.timestamp() >= limite_:
                simbolos_vistos.add(r_.get("simbolo", "").strip().lower())
    # dentro de este lote, si varios tokens comparten símbolo, solo se considera el de más liquidez
    mejor_por_simbolo = {}
    for mint_, p_ in pares.items():
        s_ = (p_["baseToken"].get("symbol") or "").strip().lower()
        liq_ = num((p_.get("liquidity") or {}).get("usd"))
        if s_ not in mejor_por_simbolo or liq_ > mejor_por_simbolo[s_][1]:
            mejor_por_simbolo[s_] = (mint_, liq_)

    pasan = []
    for mint, p in pares.items():
        sim_l = (p["baseToken"].get("symbol") or "").strip().lower()
        if BLOQUEAR_SIMBOLOS_REPETIDOS and sim_l:
            if sim_l in simbolos_vistos or mejor_por_simbolo[sim_l][0] != mint:
                continue
        m = metricas(p)
        motivos, tipo = evaluar_mercado(m)
        if motivos:
            continue
        ok, avisos, holders, top10 = evaluar_rugcheck(mint)
        time.sleep(0.5)
        sim = p["baseToken"].get("symbol")
        if not ok:
            print(f"  x {sim}: {'; '.join(avisos)}")
            continue
        pasan.append(fila_candidato(p, m, tipo, avisos, holders, top10))
        print(f"  ✓ {sim} [{tipo}] {mint}")

    añadir_csv(CAND, CAND_COLS, pasan)
    avisar_telegram(pasan)
    print(f"Candidatos que pasaron: {len(pasan)}")
    return pasan


def seguimiento():
    limite = ahora().timestamp() - HORAS_SEGUIMIENTO * 3600
    activos = {}
    for r in leer_csv(CAND):
        try:
            t = datetime.strptime(r["fecha_utc"], "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc)
        except (ValueError, KeyError):
            continue
        if t.timestamp() >= limite:
            activos[r["mint"]] = r  # si se repite, queda el más reciente
    if not activos:
        return
    pares = pares_de(list(activos))
    filas = []
    for mint, r in activos.items():
        p = pares.get(mint)
        if not p:
            continue
        entrada, actual = num(r.get("precio_usd")), num(p.get("priceUsd"))
        if not entrada:
            continue
        filas.append({
            "fecha_utc": ahora().strftime("%Y-%m-%d %H:%M"),
            "mint": mint,
            "simbolo": r.get("simbolo", ""),
            "tipo": r.get("tipo", ""),
            "precio_entrada": entrada,
            "precio_actual": actual,
            "cambio_pct": round((actual / entrada - 1) * 100, 1),
            "liquidez_usd": (p.get("liquidity") or {}).get("usd", ""),
        })
    añadir_csv(SEG, SEG_COLS, filas)
    print(f"Seguimiento actualizado: {len(filas)} tokens")


def revisar_uno(mint):
    p = pares_de([mint]).get(mint)
    if not p:
        print("DexScreener no devolvió datos para ese mint.")
        return
    m = metricas(p)
    print(f"{p['baseToken'].get('name')} ({p['baseToken'].get('symbol')})")
    print(f"  Cap/FDV: ${m['fdv']:,.0f} | Liquidez: ${m['liq']:,.0f} | Vol24h: ${m['vol24']:,.0f} | Vol1h: ${m['vol1']:,.0f}")
    print(f"  Edad: {m['edad_h']:.1f} h" if m["edad_h"] is not None else "  Edad: ?")
    print(f"  Aceleración de volumen: {m['acel']:.1f}x" if m["acel"] else "  Aceleración de volumen: -")
    print(f"  Cambio 1h: {m['cambio_h1']}% | 24h: {m['cambio_h24']}%")
    motivos, tipo = evaluar_mercado(m)
    print("Filtros de mercado:", f"PASA como '{tipo}'" if not motivos else "NO PASA")
    for x in motivos:
        print("  -", x)
    ok, avisos, holders, top10 = evaluar_rugcheck(mint)
    print(f"Seguridad (RugCheck): {'OK' if ok else 'NO PASA'} | holders: {holders} | top10: {top10}%")
    for a in avisos:
        print("  -", a)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        nuevos = escanear()
        seguimiento()
        paper_trading(nuevos)
        paper_vigilar()
    elif cmd == "check" and len(sys.argv) > 2:
        revisar_uno(sys.argv[2])
    else:
        print(__doc__)
