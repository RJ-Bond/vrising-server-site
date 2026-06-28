# ⚔ V Rising — Сайт игрового сервера

Веб-сайт для игрового сервера **V Rising** с мониторингом статуса, новостями и панелью администратора. Разворачивается одной командой на чистом Debian 13.

---

## Стек технологий

| Слой | Технология |
|------|-----------|
| Backend | Python 3.12, FastAPI (async) |
| База данных | SQLite (aiosqlite) |
| Авторизация | JWT (python-jose) + bcrypt (passlib) |
| Frontend | HTML5, Tailwind CSS (CDN), Vanilla JS |
| Reverse Proxy | Nginx |
| Контейнеризация | Docker + Docker Compose |
| ОС сервера | Debian 13 (Trixie) |

---

## Функциональность

### Главная страница
- Статус игрового сервера V Rising в реальном времени (онлайн / офлайн)
- Количество игроков, название сервера, карта, версия игры
- Данные кэшируются на 30 секунд (A2S_INFO UDP-запрос)
- Лента новостей с пагинацией и открытием полного текста в модальном окне

### Авторизация
- Регистрация и вход по логину/паролю
- JWT-токены, пароли хэшируются через bcrypt
- Две роли: `user` (игрок) и `admin` (администратор)

### Панель администратора
- Управление новостями: создание, редактирование, удаление, черновики
- Настройки сервера: IP-адрес и порт игрового сервера V Rising
- Управление пользователями: изменение роли, блокировка/разблокировка

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

После завершения в терминале появятся адрес сайта и данные для входа.

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

Отредактируйте `.env`:

```env
SECRET_KEY=замените_на_случайную_строку_32_символа
DATABASE_URL=sqlite+aiosqlite:////data/vrising.db
VRISING_SERVER_IP=127.0.0.1
VRISING_SERVER_PORT=27016
```

**4. Запустите проект**

```bash
docker compose up -d --build
```

Сайт будет доступен на порту `80`.

---

## Структура проекта

```
vrising-server-site/
├── Dockerfile               # Сборка Python-образа
├── docker-compose.yml       # Оркестрация контейнеров (web + nginx)
├── requirements.txt         # Python-зависимости
├── install.sh               # Скрипт автоустановки для Debian 13
│
├── backend/
│   ├── main.py              # Все роуты FastAPI
│   ├── models.py            # Модели БД: User, News, Setting
│   ├── database.py          # Async SQLite engine
│   ├── auth.py              # JWT + bcrypt
│   ├── monitor.py           # A2S_INFO UDP-мониторинг сервера
│   └── schemas.py           # Pydantic-схемы запросов/ответов
│
├── frontend/
│   ├── index.html           # Главная: статус + новости
│   ├── login.html           # Вход и регистрация
│   └── admin.html           # Панель администратора
│
└── nginx/
    └── nginx.conf           # /api/ → FastAPI, / → статика
```

---

## API

Документация Swagger доступна после запуска по адресу:  
`http://<IP-сервера>/api/docs`

| Метод | Роут | Доступ | Описание |
|-------|------|--------|----------|
| POST | `/api/auth/register` | Публичный | Регистрация |
| POST | `/api/auth/login` | Публичный | Вход |
| GET | `/api/auth/me` | User | Профиль |
| GET | `/api/monitor/status` | Публичный | Статус сервера |
| GET | `/api/news` | Публичный | Список новостей |
| GET | `/api/news/{slug}` | Публичный | Одна новость |
| GET/POST | `/api/admin/news` | Admin | Управление новостями |
| PUT/DELETE | `/api/admin/news/{id}` | Admin | Редактирование/удаление |
| GET/PUT | `/api/admin/settings/{key}` | Admin | Настройки |
| GET | `/api/admin/users` | Admin | Список пользователей |
| PUT | `/api/admin/users/{id}/role` | Admin | Изменить роль |
| PUT | `/api/admin/users/{id}/toggle-active` | Admin | Блок/разблок |

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

## Скриншоты

| Главная страница | Панель администратора |
|:---:|:---:|
| Мониторинг сервера и новости | Управление контентом и пользователями |

---

## Лицензия

MIT
