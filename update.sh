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

REPO="RJ-Bond/vrising-server-site"
BRANCH="master"
INSTALL_DIR="/opt/vrising-site"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║           V Rising Site — Проверка обновлений            ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

# ─── Проверка зависимостей ────────────────────────────────────────────────
command -v curl &>/dev/null || die "curl не найден. Установите: apt-get install curl"
command -v git  &>/dev/null || die "git не найден. Установите: apt-get install git"

# ─── Локальная версия ─────────────────────────────────────────────────────
if ! git -C "$SCRIPT_DIR" rev-parse --git-dir &>/dev/null; then
  die "Текущая директория не является git-репозиторием: $SCRIPT_DIR"
fi

LOCAL_HASH=$(git -C "$SCRIPT_DIR" rev-parse HEAD)
LOCAL_SHORT=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD)
LOCAL_DATE=$(git -C "$SCRIPT_DIR" log -1 --format="%ci" | cut -d' ' -f1,2 | cut -d':' -f1,2)

log "Локальная версия:  ${LOCAL_SHORT} (${LOCAL_DATE})"

# ─── Удалённая версия с GitHub ────────────────────────────────────────────
log "Проверка последней версии на GitHub..."

REMOTE_JSON=$(curl -sf "https://api.github.com/repos/${REPO}/commits/${BRANCH}" \
  -H "Accept: application/vnd.github.v3+json" 2>/dev/null) || \
  die "Не удалось подключиться к GitHub API. Проверьте интернет-соединение."

REMOTE_HASH=$(echo "$REMOTE_JSON" | grep -m1 '"sha"' | head -1 | cut -d'"' -f4)
REMOTE_SHORT="${REMOTE_HASH:0:7}"
REMOTE_DATE=$(echo "$REMOTE_JSON" | grep -m1 '"date"' | head -1 | cut -d'"' -f4 | cut -d'T' -f1)
REMOTE_MSG=$(echo "$REMOTE_JSON" | grep -m1 '"message"' | head -1 | cut -d'"' -f4 | cut -c1-60)

log "Версия на GitHub:  ${REMOTE_SHORT} (${REMOTE_DATE})"
echo ""

# ─── Сравнение ────────────────────────────────────────────────────────────
if [[ "$LOCAL_HASH" == "$REMOTE_HASH" ]]; then
  ok "У вас актуальная версия. Обновление не требуется."
  echo ""
  exit 0
fi

# Версия устарела
echo -e "${YELLOW}  Доступно обновление!${NC}"
echo ""
echo -e "  Установлено:   ${RED}${LOCAL_SHORT}${NC} (${LOCAL_DATE})"
echo -e "  Последнее:     ${GREEN}${REMOTE_SHORT}${NC} (${REMOTE_DATE})"
echo -e "  Изменение:     ${REMOTE_MSG}"
echo ""

# ─── Запрос подтверждения ─────────────────────────────────────────────────
read -rp "$(echo -e "  ${CYAN}Обновить сейчас? [y/N]:${NC} ")" CONFIRM
echo ""

if [[ ! "$CONFIRM" =~ ^[Yy]$ ]]; then
  warn "Обновление отменено."
  echo ""
  exit 0
fi

# ─── Обновление файлов ────────────────────────────────────────────────────
log "Получение обновлений с GitHub..."
git -C "$SCRIPT_DIR" pull --ff-only origin "$BRANCH"
ok "Файлы обновлены до версии $(git -C "$SCRIPT_DIR" rev-parse --short HEAD)."

# ─── Обновление файлов в INSTALL_DIR ─────────────────────────────────────
if [[ -d "$INSTALL_DIR" ]]; then
  log "Копирование обновлённых файлов в $INSTALL_DIR..."

  for f in docker-compose.yml Dockerfile requirements.txt; do
    [[ -f "$SCRIPT_DIR/$f" ]] && cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
  done

  for f in main.py models.py database.py auth.py monitor.py schemas.py __init__.py; do
    [[ -f "$SCRIPT_DIR/backend/$f" ]] && cp "$SCRIPT_DIR/backend/$f" "$INSTALL_DIR/backend/$f"
  done

  for f in index.html login.html admin.html; do
    [[ -f "$SCRIPT_DIR/frontend/$f" ]] && cp "$SCRIPT_DIR/frontend/$f" "$INSTALL_DIR/frontend/$f"
  done

  [[ -f "$SCRIPT_DIR/nginx/nginx.conf" ]] && \
    cp "$SCRIPT_DIR/nginx/nginx.conf" "$INSTALL_DIR/nginx/nginx.conf"

  ok "Файлы скопированы."

  # ─── Перезапуск контейнеров ───────────────────────────────────────────
  if command -v docker &>/dev/null && docker compose -f "$INSTALL_DIR/docker-compose.yml" ps -q 2>/dev/null | grep -q .; then
    log "Пересборка и перезапуск контейнеров..."
    docker compose -f "$INSTALL_DIR/docker-compose.yml" \
      --env-file "$INSTALL_DIR/.env" up -d --build
    ok "Контейнеры перезапущены."
  else
    warn "Контейнеры не запущены — перезапуск пропущен."
    warn "Запустите вручную: cd $INSTALL_DIR && docker compose up -d --build"
  fi
else
  warn "Директория $INSTALL_DIR не найдена — проект ещё не установлен."
  warn "Запустите сначала: sudo bash install.sh"
fi

# ─── Итог ─────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║               Обновление завершено успешно!              ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
NEW_SHORT=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD)
echo -e "  Текущая версия: ${GREEN}${NEW_SHORT}${NC}"
echo -e "  Последнее изменение: ${REMOTE_MSG}"
echo ""
