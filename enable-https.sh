#!/usr/bin/env bash
# ╔══════════════════════════════════════════════════════╗
# ║     V Rising Site — Подключение HTTPS (Let's Encrypt) ║
# ╚══════════════════════════════════════════════════════╝
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
log()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] && die "Запустите от root: sudo bash enable-https.sh"

INSTALL_DIR="/opt/vrising-site"
SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"

[[ -d "$INSTALL_DIR" ]] || die "Сайт не установлен. Сначала запустите: sudo bash install.sh"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║        V Rising Site — Настройка HTTPS (TLS)             ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# ─── 1. Получаем домен и email ────────────────────────────────────────────
if [[ $# -ge 2 ]]; then
  DOMAIN="$1"; EMAIL="$2"
else
  read -rp "  Введите домен (например, vrising.example.com): " DOMAIN
  read -rp "  Email для Let's Encrypt уведомлений: " EMAIL
fi

[[ -z "$DOMAIN" ]] && die "Домен не указан."
[[ -z "$EMAIL"  ]] && die "Email не указан."

log "Домен: $DOMAIN"
log "Email: $EMAIL"
echo ""

# ─── 2. Проверяем что домен ведёт на этот сервер ─────────────────────────
SERVER_IP=$(hostname -I | awk '{print $1}')
RESOLVED=$(dig +short "$DOMAIN" A 2>/dev/null | tail -1 || true)
if [[ "$RESOLVED" != "$SERVER_IP" ]]; then
  warn "DNS: $DOMAIN → ${RESOLVED:-не разрешается}, ожидается $SERVER_IP"
  warn "Убедитесь что A-запись домена указывает на этот сервер."
  read -rp "  Продолжить всё равно? (y/N): " CONFIRM
  [[ "${CONFIRM,,}" == "y" ]] || die "Отменено."
fi

# ─── 3. Устанавливаем certbot и dig ──────────────────────────────────────
log "Установка certbot..."
apt-get install -y -qq certbot dnsutils
ok "certbot установлен."

# ─── 4. Останавливаем nginx (освобождаем порт 80) ────────────────────────
log "Временная остановка nginx..."
docker compose -f "$INSTALL_DIR/docker-compose.yml" stop nginx 2>/dev/null || true

# ─── 5. Получаем сертификат ──────────────────────────────────────────────
log "Получение сертификата Let's Encrypt для $DOMAIN..."
certbot certonly \
  --standalone \
  --non-interactive \
  --agree-tos \
  --email "$EMAIL" \
  -d "$DOMAIN" \
  --keep-until-expiring
ok "Сертификат получен: /etc/letsencrypt/live/$DOMAIN/"

# ─── 6. Генерируем SSL nginx.conf ────────────────────────────────────────
log "Создание nginx SSL конфига..."
SSL_CONF="$SCRIPT_DIR/nginx/nginx-ssl.conf"
[[ -f "$SSL_CONF" ]] || die "Файл $SSL_CONF не найден. Обновите репозиторий."
sed "s/DOMAIN/$DOMAIN/g" "$SSL_CONF" > "$INSTALL_DIR/nginx/nginx.conf"
ok "nginx.conf обновлён (HTTPS + редирект с HTTP)."

# ─── 7. Обновляем docker-compose.yml — добавляем порт 443 и сертификаты ─
log "Обновление docker-compose.yml..."
COMPOSE_FILE="$INSTALL_DIR/docker-compose.yml"

# Проверяем, не добавлен ли уже 443
if grep -q '"443:443"' "$COMPOSE_FILE" 2>/dev/null; then
  warn "Порт 443 уже настроен в docker-compose.yml"
else
  python3 - <<PYEOF
import re, sys

with open('$COMPOSE_FILE', 'r') as f:
    content = f.read()

# Add 443 port after 80
content = content.replace(
    '      - "80:80"',
    '      - "80:80"\n      - "443:443"'
)

# Add letsencrypt volume after frontend volume
content = content.replace(
    '      - ./frontend:/usr/share/nginx/html:ro',
    '      - ./frontend:/usr/share/nginx/html:ro\n      - /etc/letsencrypt:/etc/letsencrypt:ro'
)

with open('$COMPOSE_FILE', 'w') as f:
    f.write(content)

print("docker-compose.yml обновлён")
PYEOF
fi
ok "docker-compose.yml: порт 443 и /etc/letsencrypt добавлены."

# ─── 8. Запускаем контейнеры ─────────────────────────────────────────────
log "Запуск контейнеров с HTTPS..."
docker compose -f "$INSTALL_DIR/docker-compose.yml" up -d --build
sleep 3

# ─── 9. Автообновление сертификата (cron) ────────────────────────────────
log "Настройка автообновления сертификата..."
CRON_CMD="0 3 * * * certbot renew --quiet --deploy-hook 'docker compose -f $INSTALL_DIR/docker-compose.yml restart nginx'"
( crontab -l 2>/dev/null | grep -v "certbot renew"; echo "$CRON_CMD" ) | crontab -
ok "Cron настроен: обновление ежедневно в 03:00."

# ─── Итог ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║              HTTPS успешно подключён!                    ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Сайт:           ${CYAN}https://${DOMAIN}${NC}"
echo -e "  HTTP→HTTPS:     автоматический редирект"
echo -e "  Сертификат:     /etc/letsencrypt/live/${DOMAIN}/"
echo -e "  Автообновление: cron 03:00 ежедневно"
echo ""
echo -e "  Для проверки: ${YELLOW}curl -I https://${DOMAIN}${NC}"
echo ""
