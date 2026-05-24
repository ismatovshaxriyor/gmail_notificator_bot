# Gmail -> Telegram Forward Bot

Bu bot bitta Gmail akkauntni kuzatadi va siz belgilagan sender (`SENDER_FILTER`) dan kelgan yangi xatlarni Telegram guruhga yuboradi.
Orderga o'xshash email (Order ID, Price, route, Vehicle, Carrier) bo'lsa, bot bu maydonlarni ajratib, strukturali xabar yuboradi.

## Texnologiyalar

- `python-telegram-bot`
- `peewee + sqlite`
- `.env` konfiguratsiya
- Gmail API (`OAuth2`, bir martalik ruxsat)

## 1) O'rnatish

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 2) Google OAuth tayyorlash

1. [Google Cloud Console](https://console.cloud.google.com/) da project oching.
2. `Gmail API` ni yoqing.
3. `OAuth consent screen` ni sozlang.
4. `Credentials` -> `Create Credentials` -> `OAuth client ID` -> `Desktop app` yarating.
5. Yuklab olingan faylni loyiha ildiziga `credentials.json` nomi bilan qo'ying.

## 3) .env sozlash

```bash
cp .env.example .env
```

Minimal maydonlar:

- `BOT_TOKEN`
- `ADMIN_USER_IDS` (masalan `123456789,987654321`)
- `SENDER_FILTER` (masalan `alerts@company.com`)
- `SUBJECT_MUST_CONTAIN` (masalan `New request from`)
- `TARGET_CHAT_ID` (ixtiyoriy, bir guruhni auto ulash uchun)

Target guruhlar asosan bot ichidagi `/groups` InlineButton panelidan boshqariladi.

## 4) Bir martalik Gmail auth

```bash
python main.py --auth-only
```

Bu komandadan keyin terminalda authorization link chiqadi, linkni ochib Google login/ruxsat berasiz. Muvaffaqiyatli bo'lsa `token.json` yaratiladi.

## 5) Botni ishga tushirish

```bash
python main.py
```

## Bot commandlari

- `/start` - welcome + menyu
- `/menu` - asosiy inline boshqaruv paneli
- `/help` - qisqa yo'riqnoma
- `/setsender sender@gmail.com` - filtr emailni o'zgartirish
- `/groups` - guruhlarni InlineButton bilan qo'shish/o'chirish
- `/status` - sozlamalarni ko'rish
- `/history [n]` - oxirgi yuborilgan xatlar
- `/checknow` - darhol Gmail tekshiruv

## Ma'lumotlar bazasi

`DB_PATH` dagi SQLite fayl ichida:

- `BotSetting` - konfiguratsion qiymatlar (`sender_filter`, `last_checked_ts`)
- `Group` - forward qilinadigan guruhlar ro'yxati
- `EmailHistory` - email tarixi
- `EmailDelivery` - har bir emailning guruhlar bo'yicha yetkazilish tarixi

## Eslatma

- Bot Gmailni `POLL_INTERVAL_SECONDS` bo'yicha periodik tekshiradi.
- Duplicate yuborishni oldini olish uchun `gmail_message_id` uniq saqlanadi.
- Barcha komandalar faqat `ADMIN_USER_IDS` dagi userlarga ochiq.

## Serverga qo'yish (`/var/www` + `systemd`)

Quyidagi misol Ubuntu/Debian server uchun.

### 1) Serverga clone qilish

```bash
cd /var/www
sudo git clone https://github.com/ismatovshaxriyor/gmail_notificator_bot.git
cd /var/www/gmail_notificator_bot
```

### 2) Python va virtualenv

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 3) Konfiguratsiya fayllari

```bash
cp .env.example .env
```

`.env` ichida kamida:
- `BOT_TOKEN`
- `ADMIN_USER_IDS`
- `SENDER_FILTER`
- `SUBJECT_MUST_CONTAIN=New request from`

`credentials.json` va `token.json` ni ham loyiha ildiziga qo'ying.

### 4) Ruxsatlar

```bash
sudo chown -R www-data:www-data /var/www/gmail_notificator_bot
```

### 5) systemd service ulash

```bash
sudo cp deploy/systemd/gmail-notificator-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable gmail-notificator-bot
sudo systemctl start gmail-notificator-bot
```

### 6) Holatini tekshirish

```bash
sudo systemctl status gmail-notificator-bot
sudo journalctl -u gmail-notificator-bot -f
```
