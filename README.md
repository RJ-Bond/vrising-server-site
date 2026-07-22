🇬🇧 [English version](README.en.md)

# ⚔ V Rising — Сайт игрового сервера

Веб-сайт для игровых серверов **V Rising**: мониторинг серверов в реальном времени, новости с комментариями/реакциями/опросами, лидерборд игроков (по времени в игре и по очкам), кланы (синхронизируются напрямую из игры), баны и апелляции, события/турниры, магазин на игровых очках, личные сообщения и уведомления, а также полноценная 4-уровневая панель администратора. Отдельная часть системы — интеграция с игровым сервером через компаньон-плагин BepInEx (регистрация из игры, античит-модерация, рестарты по расписанию, синхронизация кланов). Разворачивается одной командой на чистом Debian 13, обновляется командой `js` (или `vrising`).

---

## Стек технологий

| Слой | Технология |
|------|-----------|
| Backend | Python 3.12, FastAPI (async) |
| База данных | SQLite (aiosqlite), SQLAlchemy 2.0 (async ORM) |
| Авторизация | JWT (python-jose) + bcrypt (passlib), опционально 2FA/TOTP (pyotp) |
| Rate limiting | slowapi |
| Email (восстановление пароля и др.) | aiosmtplib (опционально, через SMTP) |
| Frontend | HTML5, Tailwind CSS (локальный статический билд), Vanilla JS, Canvas 2D (графики) |
| ИИ-чат | Anthropic API ("Управляющий замком", опционально) |
| Игровая интеграция | Компаньон-плагин BepInEx (`vrising-bepinex-plugin`) — X-Plugin-Key HTTP API |
| Мониторинг серверов | Steam A2S_INFO (UDP) |
| Reverse Proxy | Nginx (+ опционально HTTPS через Let's Encrypt) |
| Контейнеризация | Docker + Docker Compose |
| ОС сервера | Debian 13 (Trixie) |

---

## Функциональность

### Главная страница (`index.html`)
- Виджет мониторинга обоих игровых серверов в реальном времени (онлайн/офлайн, число игроков, карта, версия), с кнопкой подключения через `steam://rungameid/...`
- Обратный отсчёт/история вайпов
- Лента новостей: пагинация, теги, закреплённые посты, реакции, комментарии (с ответами и реакциями на комментарии), опросы, прикреплённые к новости, модальное окно с полным текстом
- Индикатор "кто сейчас на сайте" (presence, `/api/online`)
- Колокольчик уведомлений (ответы на комментарии, упоминания) и виджет личных сообщений — для авторизованных пользователей
- Опциональный ИИ-чат «Управляющий замком» (Anthropic API)
- Боковая навигация на все разделы сайта

### Сервера (`servers.html`)
- Подробный мониторинг каждого из серверов: статус, список игроков онлайн
- Интерактивные графики истории онлайна (Canvas, сглаженные кривые) с переключением периода
- Часовая тепловая карта активности (heatmap), привязанная к выбранному периоду
- История и статистика вайпов

### Игроки (`leaderboard.html`)
- Два режима лидерборда: по суммарному времени в игре и по балансу игровых очков (переключатель "⏱ Время / 💎 Очки")
- Отдельно по каждому серверу; переключение периода: всё время / месяц / неделя (для режима по времени)
- Индикатор изменения места в рейтинге (▲/▼) относительно вчерашнего дня — на основе ночных снапшотов рангов
- Поиск игрока по имени (с debounce)
- Подсветка и кнопка «📍 Найти себя» — переходит на страницу со своей строкой в рейтинге
- Индикатор «онлайн сейчас» у игроков, подключённых в данный момент, длительность последней сессии
- Аватары игроков (подтягиваются по привязанному игровому аккаунту), пьедестал почёта для топ-3

### Кланы (`clans.html`)
- Состав кланов синхронизируется напрямую из игры плагином (`POST /api/plugin/clans/sync`) — раздел полностью read-only на сайте, ручного создания/редактирования кланов через веб нет
- Карточки кланов с числом участников, девизом, поиском по названию
- Модальное окно с полным составом: роли участников (лидер/офицер/участник), аватары, ссылки на профили привязанных игроков
- Сводная статистика: число кланов, суммарное число участников

### Карта (`map.html`)
- Информационный обзор регионов карты Вардоран (Farbane Woods, Dunley Farmlands, Silverlight Hills, Cursed Forest, Hallowed Mountains, Gloomrot, Brighthaven, подземелья) с уровнем опасности каждого региона

### Баны (`bans.html`)
- Список активных банов, выданных через игровую команду `.ban` (публично видны имя, сервер, оставшееся время, кто забанил, причина)
- Поиск по нику, фильтр по серверу, отметка "новый" для банов младше 24 часов
- Для администрации (роль `admin`+) таблица дополняется действиями («Разбанить»), ссылкой на историю модерации игрока и историей снятых банов
- Раздел апелляций (`admin`+): рассмотрение заявок с `appeal.html`, одобрение апелляции автоматически снимает бан
- Ссылка на подачу апелляции (`/appeal.html`)

### Апелляция на бан (`appeal.html`)
- Публичная форма подачи апелляции на активный бан — не требует входа на сайт (у забаненного игрока обычно нет доступа ни в игру, ни на сайт под своим аккаунтом): SteamID/никнейм персонажа + текст обращения (`POST /api/appeals`)

### События и турниры (`events.html`)
- Список событий сайта с типами (pvp / pve / social / other) и статусами (upcoming / active / ended / cancelled)
- Запись и отмена записи на событие для авторизованных пользователей, лимит участников
- Управление событиями (создание/редактирование/удаление, список участников) — для администрации

### Магазин (`shop.html`)
- Обмен игровых очков (начисляются за время в игре и за стрик ежедневных заходов) на предметы из каталога
- Доступен только авторизованным пользователям — баланс привязан к аккаунту сайта
- Заявка на покупку списывает очки сразу; выдача предмета — вручную администрацией в игре (`status`: pending/fulfilled/cancelled)
- Лента собственных заявок игрока со статусами

### FAQ (`faq.html`)
- Аккордеон с ответами на частые вопросы: подключение через Steam, вайпы, правила PvE, регистрация и привязка игрового имени, восстановление пароля, кланы, онлайн на серверах, сообщение о нарушениях

### Авторизация и профиль
- Регистрация и вход по логину/паролю, JWT-токены (в httpOnly-cookie), пароли хэшируются через bcrypt
- Запоминание входа (remember me), восстановление пароля по email (forgot/reset password, письма через SMTP)
- Двухфакторная аутентификация (2FA/TOTP) — включение/отключение в профиле
- Смена email и пароля
- Личный профиль (`profile.html`): биография, обложка и аватар, привязка игрового ника, история очков и заявок магазина, лента уведомлений, личные сообщения, для staff — кастомизация админ-титула и значка (badge)
- Публичные профили игроков (`user.html`) — статистика, клан, активность, аватар
- Мастер первоначальной настройки сайта (`setup.html`) при первом запуске — создаёт первого администратора

### Панель администратора (`admin.html`)
Доступ и состав видимых разделов зависят от роли (см. ниже). Основные разделы:
- Дашборд со сводной статистикой, аналитика посещаемости
- Управление новостями: создание, редактирование, удаление, черновики, отложенная публикация, закрепление, теги, шаблоны, загрузка изображений, опросы к новости
- Модерация комментариев и жалоб (reports)
- Управление событиями/турнирами
- Управление пользователями: роли (включая назначение модератора/админа/суперадмина), блокировка/разблокировка, отвязка привязанного Steam-аккаунта, принудительный разлогин (revoke sessions), список связанных аккаунтов
- Управление лидербордом (удаление записей) и кланами (только просмотр — данные из игры)
- Управление вайпами серверов
- Магазин очков: каталог товаров, очередь заявок на выдачу, ручное начисление очков игрокам
- Раздел «Плагины» — интеграция с игровым сервером: статус heartbeat по каждому серверу, шаблоны connect/disconnect-сообщений, запланированные и повторяющиеся объявления в игровом чате, разовый и ежедневный рестарт сервера по расписанию (с обратным отсчётом), консоль RCON (только суперадмин), API-ключ плагина
- Настройки сайта: название, фон, часовой пояс и формат даты/времени, видимость пунктов меню, IP/порт серверов, SMTP, установка SSL-сертификата и обновление сайта с GitHub из интерфейса (только суперадмин)
- Файловый менеджер загруженных файлов и медиатека
- Журнал аудита действий администраторов, единый журнал модерации (баны/варны/апелляции/рестарты одной лентой), журнал ошибок
- Управление запросами на восстановление пароля
- Резервные копии базы данных: список, скачивание, создание вручную (только суперадмин)

### Игровая интеграция (BepInEx-плагин)
Компаньон-мод `vrising-bepinex-plugin` для сервера V Rising обменивается данными с сайтом через HTTP API, защищённый заголовком `X-Plugin-Key` (`backend/routers/plugin_integration.py`, часть эндпоинтов модерации — в `backend/routers/moderation.py`). Возможности:
- Регистрация и вход на сайт прямо из игрового чата (`.register`/`.login`), привязка SteamID к аккаунту сайта
- Heartbeat и учёт времени в игре (playtime) для лидерборда и начисления очков
- Стрик ежедневных заходов (streak) с начислением бонусных очков
- Приветственное сообщение о необходимости принять правила сервера (`.accept`) при первом входе
- Модерация прямо из игры: `.warn` (предупреждения), `.ban`/`.unban` (баны, временные и постоянные), синхронизация с публичным списком банов и апелляциями на сайте
- Плановые и повторяющиеся рестарты сервера (разовые и ежедневные по времени), с рассылкой предупреждений в чат
- Синхронизация состава кланов из игры на сайт
- Рассылка объявлений в игровой чат по расписанию, заданному в админке
- Персональные connect/disconnect-шаблоны сообщений на сервер

---

## Роли и права доступа

Иерархия из 4 уровней (`backend/auth.py`, `ROLE_LEVELS`), каждый следующий уровень включает права предыдущего:

| Роль | Уровень | Права |
|------|---------|-------|
| `user` | 0 | Обычный игрок: профиль, комментарии, реакции, участие в событиях, магазин, сообщения |
| `moderator` | 1 | + модерация комментариев и жалоб, управление пользователями (роли ниже своей, блокировка), лидерборд |
| `admin` | 2 | + новости, события, настройки сайта, кланы (просмотр), баны/апелляции, магазин, плагины/интеграция, файлы, аналитика, журналы |
| `superadmin` | 3 | + управление ролями администраторов, резервные копии, установка SSL, обновление сайта из интерфейса, RCON |

`admin.html` скрывает разделы бокового меню по этой же иерархии (`SECTION_MIN_ROLE` в JS), а бэкенд проверяет права через `role_level()`/`is_at_least()` — никогда через прямое сравнение со строкой `"admin"`.

---

## Быстрый старт — Debian 13

### Автоматическая установка (рекомендуется)

```bash
git clone https://github.com/RJ-Bond/vrising-server-site.git
cd vrising-server-site
sudo bash install.sh
```

Скрипт автоматически:
1. Обновит пакеты системы
2. Установит Docker и Docker Compose (официальный репозиторий)
3. Настроит брандмауэр UFW (порты 80, 443, SSH)
4. Соберёт и запустит контейнеры
5. Создаст администратора и базу данных
6. Зарегистрирует системные команды `js`/`vrising` (обновление) и `vrising-https` (выпуск SSL) — обе команды-симлинки указывают на один и тот же `install.sh`

После завершения в терминале появятся адрес сайта и данные для входа.

### Обновление

На сервере, где сайт уже установлен (`/opt/vrising-site`), для обновления до последней версии из репозитория достаточно выполнить:

```bash
sudo js
```

(эквивалент — `sudo vrising`, обе команды идентичны). Команда подтянет изменения из GitHub, пересоберёт и перезапустит контейнеры.

### HTTPS

```bash
sudo vrising-https domain.com admin@email.com
```

Выпускает и подключает SSL-сертификат Let's Encrypt для указанного домена.

---

### Ручная установка

**1. Установите Docker**

```bash
curl -fsSL https://get.docker.com | sh
```

**2. Клонируйте репозиторий**

```bash
git clone https://github.com/RJ-Bond/vrising-server-site.git
cd vrising-server-site
```

**3. Создайте файл `.env`**

```bash
cp .env.example .env
```

Отредактируйте `.env` (базовый набор — см. `.env.example`):

```env
SECRET_KEY=замените_на_случайную_строку_32_символа
DATABASE_URL=sqlite+aiosqlite:////data/vrising.db
VRISING_SERVER_IP=127.0.0.1
VRISING_SERVER_PORT=27016
ANTHROPIC_API_KEY=опционально_для_чата_управляющий_замком
```

При необходимости `docker-compose.yml` дополнительно поддерживает (не обязательны, есть значения по умолчанию): `ALLOWED_ORIGINS` (CORS), `SMTP_HOST`/`SMTP_PORT`/`SMTP_USER`/`SMTP_PASS`/`SMTP_FROM` (для писем восстановления пароля).

**4. Запустите проект**

```bash
docker compose up -d --build
```

Сайт будет доступен на порту `80`.

---

## Структура проекта

```
vrising-server-site/
├── Dockerfile                 # Сборка Python-образа
├── docker-compose.yml         # Оркестрация контейнеров (web + nginx)
├── requirements.txt           # Python-зависимости (прод)
├── requirements-dev.txt       # + pytest/pytest-asyncio для тестов
├── install.sh                 # Скрипт автоустановки/обновления для Debian 13 (js/vrising)
├── enable-https.sh            # Скрипт выпуска SSL-сертификата (vrising-https)
├── VERSION                    # Версия, отдаётся через GET /api/version
├── .env.example                # Шаблон переменных окружения
│
├── backend/
│   ├── main.py                # Оставшиеся не вынесенные в роутеры домены: версия/SEO
│   │                           # (sitemap/rss/news-embed), setup, ИИ-чат, presence (online),
│   │                           # объявления, мониторинг серверов (A2S), фоновые задачи
│   │                           # (публикация по расписанию, автобэкап, чистка, снапшоты
│   │                           # лидерборда, обновление статусов событий)
│   ├── models.py               # Модели БД: User, News, Comment, Setting, PlayerRecord,
│   │                           # Wipe, GameClan(+Member), Event, Ban/BanAppeal/Warning,
│   │                           # PointsTransaction/ShopItem/ShopRedemption и др.
│   ├── database.py             # Async SQLite engine
│   ├── auth.py                 # JWT + bcrypt + 2FA/TOTP, ROLE_LEVELS/role_level()/is_at_least()
│   ├── monitor.py              # A2S_INFO UDP-мониторинг серверов
│   ├── schemas.py              # Pydantic-схемы запросов/ответов
│   ├── helpers.py              # Общие хелперы, используемые несколькими роутерами
│   ├── rate_limit.py           # Настройка slowapi
│   ├── routers/                # Роутеры FastAPI, подключаемые через app.include_router()
│   │   ├── auth.py              #   /api/auth/* — регистрация/вход/2FA/смена пароля-email
│   │   ├── profile.py            #   /api/profile/* — био, обложка, значок, /api/team
│   │   ├── users.py              #   /api/users/*, /api/admin/users/* — профили и админ. пользователей
│   │   ├── clans.py              #   /api/clans* — игровые кланы (read-only)
│   │   ├── leaderboard.py        #   /api/leaderboard*
│   │   ├── news.py               #   /api/news*, /api/comments*, /api/admin/news*
│   │   ├── wipes.py              #   /api/wipes, /api/admin/wipes
│   │   ├── events.py             #   /api/events*, /api/admin/events*
│   │   ├── polls.py              #   /api/news/{slug}/poll*
│   │   ├── notifications.py      #   /api/notifications*
│   │   ├── messages.py           #   /api/messages* (личные сообщения)
│   │   ├── reports.py            #   /api/reports, /api/admin/reports*
│   │   ├── points_shop.py        #   /api/shop/*, /api/admin/shop/*, /api/admin/points/*
│   │   ├── moderation.py         #   /api/bans, /api/appeals, /api/admin/bans|appeals|moderation-log,
│   │   │                          #   /api/plugin/warn|ban|unban|due-unbans|ban-status|log-action
│   │   ├── plugin_integration.py #   /api/plugin/* — регистрация/heartbeat/playtime/рестарты/клан-синк
│   │   ├── server_admin.py       #   /api/admin/servers/*, /api/admin/message-templates,
│   │   │                          #   /api/admin/server-api-key
│   │   ├── admin_settings.py     #   /api/settings/public, /api/admin/settings*, /api/admin/maintenance/*
│   │   ├── admin_system.py       #   /api/admin/upload|uploads|media|backup(s)|ssl|update|rcon
│   │   └── admin_misc.py         #   /api/admin/stats|comments|audit-log|analytics|export/*|errors
│   └── tests/                    # pytest-набор (backend/tests/test_*.py + conftest.py)
│
├── frontend/                     # Без сборки — раздаётся nginx как есть
│   ├── index.html                 # Главная: мониторинг + новости + чат + presence
│   ├── servers.html               # Подробный мониторинг серверов и графики
│   ├── leaderboard.html           # Лидерборд (время / очки)
│   ├── clans.html                 # Кланы (синхронизированы из игры)
│   ├── map.html                    # Обзор карты мира
│   ├── bans.html                    # Баны + апелляции (admin)
│   ├── appeal.html                  # Публичная форма апелляции на бан
│   ├── events.html                  # События и турниры
│   ├── shop.html                     # Магазин игровых очков
│   ├── faq.html                       # FAQ
│   ├── login.html                      # Вход и регистрация
│   ├── setup.html                       # Первоначальная настройка сайта
│   ├── profile.html                      # Личный профиль
│   ├── user.html                          # Публичный профиль игрока
│   ├── reset.html                          # Восстановление пароля
│   ├── admin.html                           # Панель администратора
│   ├── maintenance.html                      # Страница техработ (503)
│   ├── offline.html                           # Офлайн-страница service worker'а
│   ├── 404.html                                # Страница не найдена
│   ├── theme.css / components.css / index.css  # Дизайн-система (токены/общие компоненты/главная)
│   ├── common.js / index.js / sw.js              # Общий JS / JS главной / service worker
│   └── tailwind.min.css, quill*, purify.min.js    # Вендоренные локально сторонние библиотеки
│
├── nginx/
│   ├── nginx.conf              # /api/ → FastAPI, / → статика, maintenance-режим, SEO-prerender для ботов
│   └── nginx-ssl.conf          # Вариант конфигурации с HTTPS (используется docker-compose)
│
└── scripts/                     # Инструменты для разработки (см. CLAUDE.md)
    ├── check.sh                  # Валидация HTML/CSS фронтенда
    ├── check_backend.sh          # Импорт всех backend-модулей через uv (ловит синтаксис/импорты)
    ├── test_backend.sh           # Запуск pytest-набора
    ├── preview.sh / preview-admin.sh / preview-mock.sh  # Headless-скриншоты страниц
    ├── admin-mock-fetch.js / public-mock-fetch.js        # Моки API для preview-скриптов
    └── serve.ps1                                          # Статический сервер frontend/ для превью
```

---

## API

Документация Swagger доступна после запуска по адресу:
`http://<IP-сервера>/api/docs`

Ниже — репрезентативный срез (в проекте 195+ роутов); полный список — в Swagger или соответствующих файлах `backend/routers/*.py` и `backend/main.py`.

| Раздел | Примеры роутов | Доступ |
|--------|------------------|--------|
| Авторизация | `POST /api/auth/register`, `/login`, `/logout`, `GET /auth/me`, `POST /auth/change-password`, `/change-email`, `/auth/2fa/setup`\|`enable`\|`disable`, `/auth/forgot-password`, `/auth/reset-password/{token}`, `/auth/avatar` | Публичный / User |
| Первоначальная настройка | `GET /api/setup/status`, `POST /api/setup/complete` | Публичный (до первой настройки) |
| Мониторинг серверов | `GET /api/monitor/status[2]`, `/monitor/history[2]`, `/monitor/snapshots`, `/monitor/stats`, `/monitor/status/stream` (SSE) | Публичный |
| Присутствие | `POST /api/online/ping`, `GET /api/online`, `/online/stream` (SSE) | Публичный |
| Новости | `GET /api/news`, `/news/{slug}`, `/news/tags`, `POST /news/{slug}/react`, `/news/{slug}/comments`, `/news/{slug}/poll`, `/poll/vote` | Публичный / User |
| Вайпы | `GET /api/wipes`, `POST/DELETE /api/admin/wipes` | Публичный / Admin |
| Лидерборд | `GET /api/leaderboard`, `/leaderboard/points`, `DELETE /api/admin/leaderboard/{id}` | Публичный / Admin |
| Кланы (игровые) | `GET /api/clans`, `/clans/{id}` | Публичный |
| События | `GET /api/events`, `/events/{id}`, `POST /events/{id}/join`, `DELETE /events/{id}/leave`, `POST/PUT/DELETE /api/admin/events` | Публичный / User / Admin |
| Магазин очков | `GET /api/shop/items`, `POST /shop/redeem`, `GET /shop/redemptions/me`, `/points/transactions/me`, `POST/PUT/DELETE /api/admin/shop/items`, `/admin/shop/redemptions/{id}/fulfill`, `POST /admin/points/grant` | Публичный / User / Admin |
| Баны и апелляции | `GET /api/bans`, `POST /api/appeals`, `GET/POST /api/admin/bans`, `/admin/bans/{id}/unban`, `/admin/appeals`, `/admin/appeals/{id}/resolve`, `/admin/moderation-log` | Публичный / Admin |
| Профили | `GET /api/users/{username}`, `/users/{username}/activity`, `POST /api/profile/bio`\|`cover`\|`badge-icon`, `GET /api/team` | Публичный / User |
| Уведомления и сообщения | `GET /api/notifications`, `POST /notifications/read-all`, `POST /api/messages`, `GET /messages/inbox`, `/messages/with/{username}` | User |
| Жалобы | `POST /api/reports`, `GET/PATCH /api/admin/reports` | User / Moderator |
| ИИ-чат | `POST /api/chat` | User |
| Игровой плагин (X-Plugin-Key) | `GET /api/plugin/status`, `POST /plugin/register`\|`login`\|`heartbeat`\|`sessions`\|`connect-streak`, `GET /plugin/wipe-info`\|`playtime`\|`restart-status`, `POST /plugin/warn`\|`ban`\|`unban`\|`clans/sync`\|`schedule-restart` | Плагин (ключ) |
| Админ: контент | `GET/POST/PUT/DELETE /api/admin/news`, `/admin/comments`, `/admin/upload`, `/admin/uploads`, `/admin/media` | Admin |
| Админ: пользователи | `GET /api/admin/users`, `PUT /api/admin/users/{id}/role`, `/toggle-active`, `/revoke-sessions`, `/unlink-steam`, `DELETE` | Moderator / Admin |
| Админ: настройки | `GET/PUT /api/admin/settings`, `/admin/settings/import`, `/admin/maintenance/status`, `/admin/server-api-key`, `/admin/message-templates` | Admin |
| Админ: рестарты и объявления | `GET/POST/DELETE /api/admin/servers/{n}/restart`, `/daily-restart`, `GET/POST/PUT/DELETE /api/admin/announcements`, `/plugin-status` | Admin |
| Админ: служебное | `GET /api/admin/stats`, `/admin/audit-log`, `/admin/analytics`, `/admin/errors`, `/admin/password-resets`, `/admin/export/*` | Admin |
| Админ: суперадмин | `POST /api/admin/ssl/install`, `/admin/update`, `/admin/rcon`, `GET /admin/backup`, `/admin/backups`, `POST /admin/backups/create` | Superadmin |
| Служебное/SEO | `GET /api/version`, `/api/sitemap.xml`, `/api/rss.xml`, `/api/news-embed` | Публичный |

---

## Управление контейнерами

```bash
# Просмотр логов
docker compose logs -f

# Перезапуск
docker compose restart

# Остановка
docker compose down

# Пересборка после изменений
docker compose up -d --build

# Обновление с GitHub (на сервере)
sudo js
```

---

## Данные по умолчанию

После первого запуска автоматически создаются:

| Параметр | Значение |
|----------|---------|
| Логин | `admin` |
| Пароль | `supersecretpassword` |

> **Смените пароль администратора сразу после первого входа.**

---

## Лицензия

MIT
