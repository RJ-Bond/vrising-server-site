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

SCRIPT_DIR="$(cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")" && pwd)"
INSTALL_DIR="/opt/vrising-site"
ADMIN_PASS="supersecretpassword"
BRANCH="master"

# ════════════════════════════════════════════════════════════════════════════
# ШАГ 0 — СИНХРОНИЗАЦИЯ С GITHUB (всегда, при любом запуске)
# ════════════════════════════════════════════════════════════════════════════
echo ""
echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${CYAN}║             V Rising Site — Синхронизация                ║${NC}"
echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""

if ! command -v git &>/dev/null; then
  warn "git не найден — пропуск синхронизации с GitHub."
elif ! git -C "$SCRIPT_DIR" rev-parse --git-dir &>/dev/null; then
  warn "Директория '$SCRIPT_DIR' не является git-репозиторием — пропуск синхронизации."
else
  HASH_BEFORE=$(git -C "$SCRIPT_DIR" rev-parse HEAD)
  SHORT_BEFORE=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD)
  log "Локальная версия до обновления: ${SHORT_BEFORE}"

  # Self-heal servers already caught by a since-fixed bug in this very script: an
  # earlier version of copy_project_files() below used to overwrite VERSION with a raw
  # git hash, which — now that VERSION is CI-managed (bumped + committed automatically
  # on every release) — left it locally modified and made THIS git pull fail on every
  # run after the first ("Ваши локальные изменения в VERSION будут перезаписаны"),
  # silently falling back to stale files forever. VERSION never legitimately needs a
  # local edit preserved, so a stray modification to just that one file is always safe
  # to discard before pulling.
  if ! git -C "$SCRIPT_DIR" diff --quiet -- VERSION 2>/dev/null; then
    warn "VERSION изменён локально (известный баг старых версий этого скрипта) — сбрасываем перед git pull."
    git -C "$SCRIPT_DIR" checkout -- VERSION 2>/dev/null || true
  fi

  log "Получение последних изменений с GitHub (git pull)..."
  if git -C "$SCRIPT_DIR" pull --ff-only origin "$BRANCH" 2>&1; then
    HASH_AFTER=$(git -C "$SCRIPT_DIR" rev-parse HEAD)
    SHORT_AFTER=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD)

    if [[ "$HASH_BEFORE" == "$HASH_AFTER" ]]; then
      ok "Файлы актуальны. Версия: ${SHORT_AFTER}"
    else
      ok "Файлы обновлены: ${RED}${SHORT_BEFORE}${NC} → ${GREEN}${SHORT_AFTER}${NC}"
      UPDATED_FILES=$(git -C "$SCRIPT_DIR" diff --name-only "$HASH_BEFORE" "$HASH_AFTER" 2>/dev/null | head -10 | sed 's/^/    /')
      if [[ -n "$UPDATED_FILES" ]]; then
        echo -e "${CYAN}  Изменённые файлы:${NC}"
        echo "$UPDATED_FILES"
      fi
    fi
  else
    warn "git pull завершился с ошибкой — используем локальные файлы."
  fi
  echo ""
fi

# ─── Вспомогательные функции ─────────────────────────────────────────────

copy_project_files() {
  mkdir -p "$INSTALL_DIR"/{backend,frontend,nginx}

  # VERSION used to get overwritten here with a raw git hash — harmless before the CI
  # release workflow existed, since nothing else cared about its committed content. Now
  # that CI bumps + commits a real semver value to VERSION on every release, writing to
  # it here (when $SCRIPT_DIR == $INSTALL_DIR, the normal case — see the branch below)
  # left the git working tree locally modified, which made THIS SAME SCRIPT'S OWN "git
  # pull --ff-only" in step 0 fail on every subsequent run ("Ваши локальные изменения в
  # VERSION будут перезаписаны"), silently falling back to stale local files forever
  # after. git pull already brings VERSION to whatever's tracked upstream — nothing left
  # to do here.

  if [[ "$(readlink -f "$SCRIPT_DIR")" == "$(readlink -f "$INSTALL_DIR")" ]]; then
    ok "Исходники и $INSTALL_DIR — одна и та же директория, git pull уже обновил файлы на месте."
    chmod +x "$INSTALL_DIR/enable-https.sh" "$INSTALL_DIR/install.sh"
    return 0
  fi

  for f in docker-compose.yml Dockerfile requirements.txt VERSION enable-https.sh install.sh; do
    [[ -f "$SCRIPT_DIR/$f" ]] || die "Файл не найден: $SCRIPT_DIR/$f"
    cp "$SCRIPT_DIR/$f" "$INSTALL_DIR/$f"
  done
  chmod +x "$INSTALL_DIR/enable-https.sh" "$INSTALL_DIR/install.sh"

  shopt -s nullglob
  backend_files=("$SCRIPT_DIR"/backend/*.py)
  [[ ${#backend_files[@]} -gt 0 ]] || die "В $SCRIPT_DIR/backend нет .py файлов"
  cp "${backend_files[@]}" "$INSTALL_DIR/backend/"

  frontend_files=("$SCRIPT_DIR"/frontend/*)
  [[ ${#frontend_files[@]} -gt 0 ]] || die "В $SCRIPT_DIR/frontend нет файлов для копирования"
  cp "${frontend_files[@]}" "$INSTALL_DIR/frontend/"
  shopt -u nullglob

  [[ -f "$SCRIPT_DIR/nginx/nginx.conf" ]] || die "Файл не найден: $SCRIPT_DIR/nginx/nginx.conf"
  cp "$SCRIPT_DIR/nginx/nginx.conf" "$INSTALL_DIR/nginx/nginx.conf"
  [[ -f "$SCRIPT_DIR/nginx/nginx-ssl.conf" ]] && cp "$SCRIPT_DIR/nginx/nginx-ssl.conf" "$INSTALL_DIR/nginx/nginx-ssl.conf"

  ok "Файлы проекта скопированы."
}

start_containers() {
  log "Сборка и запуск контейнеров..."
  docker compose -f "$INSTALL_DIR/docker-compose.yml" --env-file "$INSTALL_DIR/.env" up -d --build

  # nginx.conf/nginx-ssl.conf are bind-mounted (docker-compose.yml), not baked into the
  # nginx image — `up -d --build` only recreates a container when its OWN compose
  # service definition changes (image/env/volumes list), so editing just the mounted
  # config file's *content* never triggers that, no matter how many times this script
  # runs. Worse: nginx resolves proxy_pass hostnames (e.g. vrising_api) to an IP ONCE,
  # at worker startup, and caches it for the container's entire lifetime (no `resolver`
  # directive configured) — so once `web` (or any upstream) gets recreated with a new
  # IP by the `up -d --build` above, a long-running nginx starts throwing 502 "Connection
  # refused" against the stale IP. This is exactly what took the site down on
  # 2026-08-02: nginx had been running 11 days untouched, never picked up ANY nginx.conf
  # change in that window, then started 502ing the moment `web` got a new IP from an
  # unrelated deploy. An unconditional restart here is cheap (nginx starts in ~1s) and
  # fixes both problems at once — new config loaded, upstream IPs re-resolved.
  log "Перезапуск nginx (подхват конфига + пересборка DNS-кэша до апстримов)..."
  docker compose -f "$INSTALL_DIR/docker-compose.yml" --env-file "$INSTALL_DIR/.env" restart nginx

  log "Ожидание запуска API (до 60 сек)..."
  for i in $(seq 1 30); do
    if curl -sf http://localhost/api/monitor/status &>/dev/null; then
      break
    fi
    sleep 2
  done

  # The `web` container already runs migrations itself on every boot, before it starts
  # accepting requests (backend/main.py's lifespan() -> backend/db_migrate.py, which
  # handles both a normal `alembic upgrade head` and — for a pre-Alembic existing DB —
  # the one-time `stamp head` adoption; see that module's docstring) — the health-wait
  # loop above having succeeded already implies migrations succeeded, since a failed
  # migration crashes the app before it can ever bind and answer that health check. This
  # call runs strictly AFTER that loop (never concurrently with the container's own
  # migration attempt, which would otherwise mean two `alembic` processes racing the
  # same sqlite file) and is expected to be an instant no-op — it exists purely to give
  # `sudo js` a clear, explicit, loudly-failing line for this step rather than making an
  # operator infer "migrations must be fine" from an unrelated health check.
  log "Проверка миграций БД (alembic upgrade head)..."
  if docker compose -f "$INSTALL_DIR/docker-compose.yml" --env-file "$INSTALL_DIR/.env" exec -T web python -m backend.db_migrate; then
    ok "Миграции БД в актуальном состоянии."
  else
    die "Миграция БД завершилась ошибкой — сайт может работать со старой схемой. Проверьте: docker compose -f ${INSTALL_DIR}/docker-compose.yml logs web"
  fi
}

# ════════════════════════════════════════════════════════════════════════════
# РЕЖИМ ОБНОВЛЕНИЯ — если проект уже установлен
# ════════════════════════════════════════════════════════════════════════════
if [[ -d "$INSTALL_DIR" && -f "$INSTALL_DIR/docker-compose.yml" ]]; then

  echo -e "${CYAN}╔══════════════════════════════════════════════════════════╗${NC}"
  echo -e "${CYAN}║           V Rising Site — Обновление                     ║${NC}"
  echo -e "${CYAN}╚══════════════════════════════════════════════════════════╝${NC}"
  echo ""

  log "Копирование обновлённых файлов в $INSTALL_DIR..."
  copy_project_files

  start_containers

  SERVER_IP=$(hostname -I | awk '{print $1}')
  CURRENT_VERSION=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo "n/a")
  echo ""
  echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
  echo -e "${GREEN}║               Обновление завершено успешно!              ║${NC}"
  echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
  echo ""
  # Обновить симлинки на случай если они были удалены
  ln -sf "$INSTALL_DIR/install.sh"      /usr/local/bin/js 2>/dev/null || true
  ln -sf "$INSTALL_DIR/install.sh"      /usr/local/bin/vrising 2>/dev/null || true
  ln -sf "$INSTALL_DIR/enable-https.sh" /usr/local/bin/vrising-https 2>/dev/null || true

  echo -e "  Версия:      ${GREEN}${CURRENT_VERSION}${NC}"
  echo -e "  Сайт:        ${CYAN}http://${SERVER_IP}${NC}"
  echo -e "  Обновление:  sudo js"
  echo -e "  Логи:        docker compose -f ${INSTALL_DIR}/docker-compose.yml logs -f"
  echo ""
  exit 0
fi

# ════════════════════════════════════════════════════════════════════════════
# РЕЖИМ УСТАНОВКИ — первый запуск
# ════════════════════════════════════════════════════════════════════════════
SECRET_KEY="$(cat /proc/sys/kernel/random/uuid | tr -d '-' | head -c 40)"

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
ANTHROPIC_API_KEY=
ENV
ok ".env создан."
warn "Добавьте ANTHROPIC_API_KEY в $INSTALL_DIR/.env для активации чата 'Управляющий замком'."

# ─── 6. Симлинки в PATH ───────────────────────────────────────────────
log "Создание системных команд..."
ln -sf "$INSTALL_DIR/install.sh"      /usr/local/bin/js
ln -sf "$INSTALL_DIR/install.sh"      /usr/local/bin/vrising
ln -sf "$INSTALL_DIR/enable-https.sh" /usr/local/bin/vrising-https
ok "Команды доступны: js, vrising, vrising-https"

# ─── 7. Сборка и запуск ──────────────────────────────────────────────────
start_containers

# ─── Итог ─────────────────────────────────────────────────────────────────
SERVER_IP=$(hostname -I | awk '{print $1}')
CURRENT_VERSION=$(git -C "$SCRIPT_DIR" rev-parse --short HEAD 2>/dev/null || echo "n/a")
echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║          V Rising Site — Установка завершена!            ║${NC}"
echo -e "${GREEN}╚══════════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  Сайт доступен:    ${CYAN}http://${SERVER_IP}${NC}"
echo -e "  Панель Админа:    ${CYAN}http://${SERVER_IP}/admin.html${NC}"
echo -e "  API Docs:         ${CYAN}http://${SERVER_IP}/api/docs${NC}"
echo ""
echo -e "  Версия:                ${GREEN}${CURRENT_VERSION}${NC}"
echo -e "  Логин администратора:  ${YELLOW}${ADMIN_PASS}${NC}"
echo ""
echo -e "  Обновление:  sudo js"
echo -e "  Логи:        docker compose -f ${INSTALL_DIR}/docker-compose.yml logs -f"
echo ""
echo -e "  ${CYAN}HTTPS:${NC}       sudo vrising-https domain.com admin@email.com"
echo ""
echo -e "${YELLOW}  ВАЖНО: Смените пароль администратора после первого входа!${NC}"
echo ""
