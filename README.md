# Escáner de memecoins Solana

Descarta tokens peligrosos y marca señales de momentum. NO predice cuál sube.

## Subirlo y activarlo
1. Crea un repo en GitHub y sube TODO esto (incluida la carpeta oculta `.github`).
2. Settings -> Actions -> General -> Workflow permissions -> "Read and write permissions".
3. Pestaña Actions -> "scanner" -> Run workflow. Después corre solo cada 30 min.

## Avisos al celular (opcional, recomendado)
1. En Telegram habla con @BotFather -> /newbot -> copia el TOKEN.
2. Escríbele cualquier mensaje a tu bot nuevo, luego abre en el navegador:
   https://api.telegram.org/bot<TOKEN>/getUpdates  y copia el número de "chat":{"id":...}
3. En el repo: Settings -> Secrets and variables -> Actions -> New repository secret:
   - TELEGRAM_TOKEN   = el token
   - TELEGRAM_CHAT_ID = el id
   (Nunca pegues estos valores dentro del código.)

## Revisar un token a mano
    pip install requests
    python scanner.py check <MINT>

## Archivos en data/
- candidatos.csv      tokens que pasaron los filtros (tipo "nuevo" o "despertar")
- seguimiento.csv     precio de cada candidato durante 48 h
- mis_operaciones.csv plantilla para anotar tus operaciones reales

## Ajustes
Todos los filtros están al inicio de scanner.py (sección CONFIGURACIÓN).

## Paper trading (simulación)
Tres cuentas simuladas (cada una con $10, $2 por operación) operan los MISMOS candidatos
"nuevo" o "despertar", cada una con una regla de salida distinta, para comparar cuál rinde mejor:
- A_fija:       vende a +60% o a -35%, máximo 4 h.
- B_escalonado: el stop empieza a seguir al precio máximo desde +30% (a 25% de distancia); sin tope fijo, máx. 24 h.
- C_parcial:    como B, pero vende la mitad al llegar a +100%.
Todos los parámetros están al inicio de scanner.py (sección PAPER TRADING).

Archivos en data/:
- paper_trades.csv   cada operación cerrada, con la columna "estrategia"
- paper_balance.csv  balance de cada estrategia en cada corrida (compara la columna "total")
- paper_estado.json  estado interno. Bórralo para reiniciar la simulación.

Limitación: el bot solo mira el precio cada 30 min, así que la simulación es aproximada.
Un token que desaparece sin datos se cuenta como pérdida total.


## Versión 3 (cambios tras la primera prueba)
- Solo queda la regla A_fija (TP +60%, SL -35%, máx. 4 h). B y C salieron claramente peores en memecoins.
- Vigilancia: tras cada escaneo, el bot vigila las posiciones abiertas cada 45 s durante hasta 25 min
  (antes miraba cada 30 min y un stop de -35% cerraba en -63%). Por eso el workflow ahora dura hasta ~30 min
  y tiene `timeout-minutes: 40`.
- Filtros nuevos: no entra si el token ya subió más de 150% o cae más de 20% en la última hora, y descarta
  copias (mismo símbolo con otro contrato que uno ya visto).
- paper_trades.csv ahora guarda las características de cada entrada (liquidez, volumen, holders, etc.) para
  analizar con datos qué separa a los tokens que suben de los que se desploman.
- Para reiniciar la cuenta simulada con $10: borra data/paper_estado.json.
