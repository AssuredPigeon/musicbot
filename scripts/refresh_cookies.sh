#!/usr/bin/env bash
# scripts/refresh_cookies.sh
#
# Extrae cookies frescas de YouTube desde tu navegador local
# y las sube automáticamente al servidor de Oracle Cloud.
#
# Uso:
#   bash scripts/refresh_cookies.sh
#
# Requisitos:
#   - yt-dlp instalado localmente  (pip install yt-dlp)
#   - Acceso SSH al servidor        (configurado en ~/.ssh/config o con clave)
#   - Sesión de YouTube activa en el navegador que uses
#
# Configura estas variables antes de ejecutar:
# ─────────────────────────────────────────────
SERVER_USER="opc"
SERVER_HOST="tu-ip-oracle-cloud"          # Ej: 129.80.123.45
SERVER_PATH="/home/opc/musicbot/musicbot/cookies.txt"
BROWSER="chrome"                          # chrome | firefox | edge | safari
# ─────────────────────────────────────────────

set -e

echo "🍪 Extrayendo cookies de YouTube desde $BROWSER..."

# Extraer cookies del navegador local al archivo temporal
yt-dlp \
  --cookies-from-browser "$BROWSER" \
  --skip-download \
  --cookies /tmp/yt_cookies_fresh.txt \
  "https://www.youtube.com" 2>/dev/null || true

# Verificar que se generaron cookies
if [ ! -s /tmp/yt_cookies_fresh.txt ]; then
  echo "❌ No se pudieron extraer cookies. Asegúrate de:"
  echo "   1. Tener sesión iniciada en YouTube en $BROWSER"
  echo "   2. Tener yt-dlp instalado (pip install yt-dlp)"
  exit 1
fi

echo "✅ Cookies extraídas correctamente."
echo "   $(grep -c 'youtube.com' /tmp/yt_cookies_fresh.txt) entradas de YouTube"

# Subir al servidor
echo "📤 Subiendo cookies al servidor $SERVER_HOST..."
scp /tmp/yt_cookies_fresh.txt "${SERVER_USER}@${SERVER_HOST}:${SERVER_PATH}"

if [ $? -eq 0 ]; then
  echo "✅ Cookies actualizadas en el servidor."
  echo ""
  echo "💡 El bot usará las cookies nuevas en la próxima reproducción."
  echo "   No es necesario reiniciar el bot."
else
  echo "❌ Error al subir las cookies. Verifica tu conexión SSH."
  exit 1
fi

# Limpiar archivo temporal
rm -f /tmp/yt_cookies_fresh.txt

echo ""
echo "🎵 Listo."
