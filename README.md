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
