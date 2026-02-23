# 🎭 Анонімні питання — Telegram Mini App

Повноцінний Telegram Mini App з красивим інтерфейсом прямо в Telegram.

## 📦 Структура

```
tma_anon/
├── server.py          # Flask бекенд + API
├── frontend/
│   └── index.html     # Весь UI (один файл)
├── requirements.txt   # Тільки Flask + dotenv
├── .env.example
└── README.md
```

## 🚀 Встановлення та запуск

### Крок 1 — Встановити залежності

```cmd
py -m pip install flask python-dotenv
```

> ✅ **Не потрібен компілятор C++** — тільки чистий Python!

### Крок 2 — Налаштувати .env

```cmd
copy .env.example .env
```

Відкрийте `.env` і заповніть:
```
BOT_TOKEN=токен_від_BotFather
ADMIN_IDS=ваш_telegram_id
ANON_SALT=будь_який_випадковий_рядок
DEV_MODE=1   ← для тесту на локалці
```

### Крок 3 — Запустити сервер

```cmd
py server.py
```

Сервер запуститься на `http://localhost:5000`

---

## 🌐 Публікація (обов'язково для Telegram)

Telegram Mini App вимагає **HTTPS**. Варіанти:

### A) ngrok (для тестування)
```cmd
py -m pip install pyngrok
ngrok http 5000
```
Скопіюйте URL типу `https://abc123.ngrok.io`

### B) Railway (безкоштовно)
1. [railway.app](https://railway.app) → New Project → Deploy from GitHub
2. Додайте змінні середовища з `.env`
3. Отримаєте URL типу `https://yourapp.up.railway.app`

### C) Render (безкоштовно)
1. [render.com](https://render.com) → New Web Service
2. Build command: `pip install -r requirements.txt`
3. Start command: `python server.py`

---

## 🤖 Налаштування Mini App у Telegram

1. Напишіть [@BotFather](https://t.me/BotFather)
2. `/newbot` → створіть бота
3. `/mybots` → оберіть бота → **Bot Settings** → **Menu Button**
4. Введіть URL вашого сервера: `https://yourapp.railway.app`
5. Або: `/newapp` → прив'яжіть WebApp до бота

### Встановити URL в коді

У файлі `frontend/index.html` знайдіть рядок:
```js
const API = ''; // Буде замінено на URL вашого бекенду
```
Замініть на:
```js
const API = 'https://yourapp.railway.app/api';
```

---

## 🔒 Як працює анонімність

| Що відбувається | Деталі |
|----------------|--------|
| Telegram ID → хеш | BLAKE2b з секретною сіллю, необоротно |
| Адмін бачить | Тільки текст + час + номер питання |
| User ID | Зберігається для доставки відповіді, НЕ передається адміну |
| Після відповіді | Зв'язок user↔question залишається в БД для доставки, але не показується |

---

## 📡 API Endpoints

| Метод | URL | Опис |
|-------|-----|------|
| GET | `/api/me` | Профіль, кулдаун, чи адмін |
| POST | `/api/ask` | Надіслати питання |
| GET | `/api/answer/:id` | Перевірити відповідь |
| POST | `/api/rate` | Оцінити відповідь |
| GET | `/api/admin/stats` | Статистика (адмін) |
| GET | `/api/admin/questions` | Список питань (адмін) |
| POST | `/api/admin/reply` | Відповісти на питання |
| POST | `/api/admin/delete` | Видалити питання |
| POST | `/api/admin/block` | Заблокувати користувача |

---

## 🔄 Різниця між ботом і Mini App

| | Telegram Bot | Telegram Mini App |
|---|---|---|
| Інтерфейс | Кнопки в чаті | Повноцінний WebApp |
| Анімації | ❌ | ✅ |
| Складні форми | ❌ | ✅ |
| Статистика | Текстова | Красиві картки |
| Встановлення | Просте | Потрібен HTTPS сервер |
