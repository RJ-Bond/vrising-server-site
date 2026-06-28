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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="/opt/vrising-site"
ADMIN_PASS="supersecretpassword"
REPO="RJ-Bond/vrising-server-site"
BRANCH="master"

# ─── Функция копирования файлов проекта ──────────────────────────────────
copy_project_files() {
  mkdir -p "$INSTALL_DIR"/{backend,frontend,nginx}

  for f in docker-compose.yml Dockerfile requirements.txt; do
    [[ -f "$SCRIPT_DIR/$f" ]] || die "Файл не найден: $SCRIPT_DIR/$f"
    cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
  done

  for f in main.py models.py database.py auth.py monitor.py schemas.py __init__.py; do
    [[ -f "$SCRIPT_DIR/backend/$f" ]] || die "Файл не найден: $SCRIPT_DIR/backend/$f"
    cp "$SCRIPT_DIR/backend/$f" "$INSTALL_DIR/backend/$f"
  done

  for f in index.html login.html admin.html; do
    [[ -f "$SCRIPT_DIR/frontend/$f" ]] || die "Файл не найден: $SCRIPT_DIR/frontend/$f"
    cp "$SCRIPT_DIR/frontend/$f" "$INSTALL_DIR/frontend/$f"
  done

  [[ -f "$SCRIPT_DIR/nginx/nginx.conf" ]] || die "Файл не найден: $SCRIPT_DIR/nginx/nginx.conf"
  cp "$SCRIPT_DIR/nginx/nginx.conf" "$INSTALL_DIR/nginx/nginx.conf"

  ok "Файлы проекта скопированы."
}

# ─── Функция запуска / пересборки контейнеров ────────────────────────────
start_containers() {
  log "Сборка и запуск контейнеров..."
  docker compose -f "$INSTALL_DIR/docker-compose.yml" --env-file "$INSTALL_DIR/.env" up -d --build

  log "Ожидание запуска API (до 60 сек)..."
  for i in $(seq 1 30); do
    if curl -sf http://localhost/api/monitor/status &>/dev/null; then
      break
    fi
    sleep 2
  done
}

# ════════════════════════════════════════════════════════════════════════════
# РЕЖИМ ОБНОВЛЕНИЯ — если проект уже установлен
# ════════════════════════════════════════════════════════════════════════════
if [[ -d "$INSTALL_DIR" && -f "$INSTALL_DIR/docker-compose.yml" ]]; then

  echo ""
  echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
  echo -e "${CYAN}║           V Rising Site — Проверка обновлений            ║${NC}"
  echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
  echo ""

  command -v curl &>/dev/null || die "curl не найден"
  command -v git  &>/dev/null || die "git не найден"

  if ! git -C "$SCRIPT_DIR" rev-parse --git-dir &>/dev/null; then
    die "Директория скрипта не является git-репозиторием: $SCRIPT_DIR"
  fi

  LOCAL_HASH=$(git -C "$SCRIPT_DIR" rev-parse HEAD)
  LOCAL_SHORT=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD)
  LOCAL_DATE=$(git -C "$SCRIPT_DIR" log -1 --format="%ci" | cut -d' ' -f1,2 | cut -d':' -f1,2)
  log "Локальная версия:  ${LOCAL_SHORT} (${LOCAL_DATE})"

  log "Проверка последней версии на GitHub..."
  REMOTE_JSON=$(curl -sf "https://api.github.com/repos/${REPO}/commits/${BRANCH}" \
    -H "Accept: application/vnd.github.v3+json" 2>/dev/null) || \
    die "Не удалось подключиться к GitHub API."

  REMOTE_HASH=$(echo "$REMOTE_JSON" | grep -m1 '"sha"' | head -1 | cut -d'"' -f4)
  REMOTE_SHORT="${REMOTE_HASH:0:7}"
  REMOTE_DATE=$(echo "$REMOTE_JSON" | grep -m1 '"date"' | head -1 | cut -d'"' -f4 | cut -d'T' -f1)
  REMOTE_MSG=$(echo "$REMOTE_JSON"  | grep -m1 '"message"' | head -1 | cut -d'"' -f4 | cut -c1-60)
  log "Версия на GitHub:  ${REMOTE_SHORT} (${REMOTE_DATE})"
  echo ""

  if [[ "$LOCAL_HASH" == "$REMOTE_HASH" ]]; then
    ok "У вас актуальная версия. Обновление не требуется."
    echo ""
    exit 0
  fi

  echo -e "${YELLOW}  Доступно обновление!${NC}"
  echo ""
  echo -e "  Установлено:  ${RED}${LOCAL_SHORT}${NC} (${LOCAL_DATE})"
  echo -e "  Последнее:    ${GREEN}${REMOTE_SHORT}${NC} (${REMOTE_DATE})"
  echo -e "  Изменение:    ${REMOTE_MSG}"
  echo ""
  read -rp "$(echo -e "  ${CYAN}Обновить сейчас? [y/N]:${NC} ")" CONFIRM
  echo ""

  if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
    warn "Обновление отменено."
    echo ""
    exit 0
  fi

  log "Получение обновлений с GitHub..."
  git -C "$SCRIPT_DIR" pull --ff-only origin "$BRANCH"
  ok "Репозиторий обновлён до $(git -C "$SCRIPT_DIR" rev-parse --short HEAD)."

  copy_project_files
  start_containers

  echo ""
  echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}║               Обновление завершено успешно!              ║${NC}"
  echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
  echo ""
  echo -e "  Версия: ${GREEN}$(git -C "$SCRIPT_DIR" rev-parse --short HEAD)${NC}"
  echo -e "  Сайт:   ${CYAN}http://$(hostname -I | awk '{print $1}')${NC}"
  echo ""
  exit 0
fi

# ════════════════════════════════════════════════════════════════════════════
# РЕЖИМ УСТАНОВКИ — первый запуск
# ════════════════════════════════════════════════════════════════════════════
SECRET_KEY="$(cat /proc/sys/kernel/random/uuid | tr -d '-' | head -c 40)"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║            V Rising Site — Автоустановка                 ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
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
apt-get install -y -qq ufw
UFW=/usr/sbin/ufw
"$UFW" allow OpenSSH
"$UFW" allow 80/tcp
"$UFW" allow 443/tcp
"$UFW" --force enable
ok "Брандмауэр настроен (SSH, HTTP, HTTPS)."

# ─── 5. Файлы проекта ────────────────────────────────────────────────────
log "Копирование файлов в $INSTALL_DIR..."
copy_project_files

# ─── .env (создаётся только при первой установке) ─────────────────────────
cat > "$INSTALL_DIR/.env" <<ENV
SECRET_KEY=${SECRET_KEY}
DATABASE_URL=sqlite+aiosqlite:////data/vrising.db
VRISING_SERVER_IP=127.0.0.1
VRISING_SERVER_PORT=27016
ENV
ok ".env создан."

# ─── 6. Сборка и запуск ──────────────────────────────────────────────────
start_containers

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
echo -e "  Директория:       ${INSTALL_DIR}"
echo -e "  Обновление:       sudo bash ${SCRIPT_DIR}/install.sh"
echo -e "  Логи:             docker compose -f ${INSTALL_DIR}/docker-compose.yml logs -f"
echo ""
echo -e "${YELLOW}  ВАЖНО: Смените пароль администратора после первого входа!${NC}"
echo ""
