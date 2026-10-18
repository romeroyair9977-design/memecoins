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

IMPORTANTE: esto NO predice cuál sube. Descarta lo peligroso y marca señales de momentum.
"""

import csv
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
# ================================================================================

DS = "https://api.dexscreener.com"
GT = "https://api.geckoterminal.com/api/v2/networks/solana"
RUG = "https://api.rugcheck.xyz/v1"
DATA = "data"
CAND = os.path.join(DATA, "candidatos.csv")
SEG = os.path.join(DATA, "seguimiento.csv")

CAND_COLS = [
    "fecha_utc", "mint", "simbolo", "nombre", "tipo", "precio_usd", "liquidez_usd",
    "vol_h24", "vol_h1", "aceleracion_vol", "cambio_h1_pct", "cambio_h24_pct", "fdv",
    "edad_h", "compras_h1", "ventas_h1", "holders", "top10_pct", "avisos_rugcheck",
]
SEG_COLS = [
    "fecha_utc", "mint", "simbolo", "tipo", "precio_entrada", "precio_actual",
    "cambio_pct", "liquidez_usd",
]

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

    pasan = []
    for mint, p in pares.items():
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
        escanear()
        seguimiento()
    elif cmd == "check" and len(sys.argv) > 2:
        revisar_uno(sys.argv[2])
    else:
        print(__doc__)
