#!/usr/bin/env bash
set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()  { echo -e "${CYAN}[INFO]${NC}  $*"; }
ok()   { echo -e "${GREEN}[OK]${NC}    $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC}  $*"; }
die()  { echo -e "${RED}[ERROR]${NC} $*" >&2; exit 1; }

[[ $EUID -ne 0 ]] && die "Запустите скрипт от root: sudo bash install.sh"

INSTALL_DIR="/opt/vrising-site"
ADMIN_PASS="supersecretpassword"
SECRET_KEY="$(cat /proc/sys/kernel/random/uuid | tr -d '-' | head -c 40)"

log "=== V Rising Site — Автоустановка ==="
log "Директория установки: $INSTALL_DIR"

# ─── 1. Системные обновления ──────────────────────────────────────────────
log "Обновление пакетов..."
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
ok "Пакеты обновлены."

# ─── 2. Зависимости ───────────────────────────────────────────────────────
log "Установка системных утилит..."
apt-get install -y -qq curl git ufw ca-certificates gnupg lsb-release
ok "Утилиты установлены."

# ─── 3. Docker ────────────────────────────────────────────────────────────
if command -v docker &>/dev/null; then
  warn "Docker уже установлен: $(docker --version)"
else
  log "Установка Docker (официальный метод)..."
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/debian/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo \
    "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
    https://download.docker.com/linux/debian $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable docker --now
  ok "Docker установлен: $(docker --version)"
fi

# ─── 4. UFW Firewall ──────────────────────────────────────────────────────
log "Настройка UFW..."
ufw allow OpenSSH
ufw allow 80/tcp
ufw allow 443/tcp
ufw --force enable
ok "Брандмауэр настроен (SSH, HTTP, HTTPS)."

# ─── 5. Структура проекта ────────────────────────────────────────────────
log "Создание структуры проекта в $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"/{backend,frontend,nginx}
cd "$INSTALL_DIR"

# ─── .env ─────────────────────────────────────────────────────────────────
cat > "$INSTALL_DIR/.env" <<ENV
SECRET_KEY=${SECRET_KEY}
DATABASE_URL=sqlite+aiosqlite:////data/vrising.db
VRISING_SERVER_IP=127.0.0.1
VRISING_SERVER_PORT=27016
ENV
ok ".env создан."

# ─── Копируем файлы из текущей директории скрипта ─────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for f in docker-compose.yml Dockerfile requirements.txt; do
  if [[ -f "$SCRIPT_DIR/$f" ]]; then
    cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
  else
    die "Файл не найден: $SCRIPT_DIR/$f"
  fi
done

for f in main.py models.py database.py auth.py monitor.py schemas.py __init__.py; do
  if [[ -f "$SCRIPT_DIR/backend/$f" ]]; then
    cp "$SCRIPT_DIR/backend/$f" "$INSTALL_DIR/backend/$f"
  else
    die "Файл не найден: $SCRIPT_DIR/backend/$f"
  fi
done

for f in index.html login.html admin.html; do
  if [[ -f "$SCRIPT_DIR/frontend/$f" ]]; then
    cp "$SCRIPT_DIR/frontend/$f" "$INSTALL_DIR/frontend/$f"
  else
    die "Файл не найден: $SCRIPT_DIR/frontend/$f"
  fi
done

if [[ -f "$SCRIPT_DIR/nginx/nginx.conf" ]]; then
  cp "$SCRIPT_DIR/nginx/nginx.conf" "$INSTALL_DIR/nginx/nginx.conf"
else
  die "Файл не найден: $SCRIPT_DIR/nginx/nginx.conf"
fi

ok "Файлы проекта скопированы."

# ─── 6. Сборка и запуск ──────────────────────────────────────────────────
log "Сборка и запуск контейнеров..."
cd "$INSTALL_DIR"
docker compose --env-file .env up -d --build

# Ждём старта API
log "Ожидание запуска API (до 60 сек)..."
for i in $(seq 1 30); do
  if curl -sf http://localhost/api/monitor/status &>/dev/null; then
    break
  fi
  sleep 2
done

# ─── Итог ─────────────────────────────────────────────────────────────────
SERVER_IP=$(hostname -I | awk '{print $1}')
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║          V Rising Site — Установка завершена!            ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Сайт доступен:    ${CYAN}http://${SERVER_IP}${NC}"
echo -e "  Панель Админа:    ${CYAN}http://${SERVER_IP}/admin.html${NC}"
echo -e "  API Docs:         ${CYAN}http://${SERVER_IP}/api/docs${NC}"
echo ""
echo -e "  Логин администратора:  ${YELLOW}admin${NC}"
echo -e "  Пароль:                ${YELLOW}${ADMIN_PASS}${NC}"
echo ""
echo -e "  Директория:            ${INSTALL_DIR}"
echo -e "  Управление:            cd ${INSTALL_DIR} && docker compose logs -f"
echo ""
echo -e "${YELLOW}  ВАЖНО: Смените пароль администратора после первого входа!${NC}"
echo ""
