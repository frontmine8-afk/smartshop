# SMART SHOP V4 — Online shop + 2 Telegram bot

## Ishga tushirish
1. Windows uchun Python 3.10+ o'rnating.
2. `start.bat` ni ishga tushiring.
3. `http://127.0.0.1:8000` — katalog.
4. `http://127.0.0.1:8000/admin` — admin panel.
5. Birinchi startda `.env` yaratiladi. Admin parolini darhol almashtiring.

## Ikki alohida Telegram bot

### 1) Xaridor OTP bot
- BotFather'da alohida bot yarating.
- Tokenini `.env` dagi `OTP_BOT_TOKEN` ga kiriting va username ni `OTP_BOT_USERNAME` ga `@` belgisiz yozing.
- Sayt register oynasida ko'rsatilgan Saytdagi register oynasidagi “Telegram botni ochish” havolasini bosing.
- Xaridor `/start` bosadi va faqat o'zining Telegram kontaktini yuboradi.
- Telefon raqami `phone_links` jadvaliga bog'lanadi. Keyingi kodlar shu OTP bot orqali keladi.

### 2) Admin bot
- BotFather'da boshqa bot yarating.
- Tokenini `.env` dagi `BOT_TOKEN` ga kiriting.
- Adminlarning chat ID sini `ADMIN_CHAT_IDS` ga yozing (vergul bilan ajrating).
- Adminlar botga `/start` yuboradi.
- Botda `/addproduct`, `/products`, `/setstock ID SON`, `/deleteproduct ID`, `/help` ishlэнэ.

## OTP xususiyatlari
- 6 raqamli tasodifiy kod; 2 daqiqa amal qiladi.
- 60 soniyadan oldin resend yo'q; soatiga telefon boshiga 5 limit.
- Kod hash holatda saqlanadi; 5 noto'g'ri urinishdan so'ng vaqtinchalik blok.
- Telegram OTP faqat telefon raqami bot orqali o'z egasi tomonidan share qilinganidan keyin ishlaydi.
- Telegram bot foydalanuvchiga birinchi bo'lib yozolmaydi: xaridor `/start` bosishi shart.

## Eslatma
Bu paketdagi Telegram OTP — SMS emas, Telegram bot DM orqali yuboriladigan tasdiqlash kodi. Botning username havolasini foydalanuvchi interfeysida ko'rsatish uchun `OTP_BOT_USERNAME` qiymatini o'zingizning bot username'ingizga moslang. Haqiqiy SMS yuborilmaydi.

Production oldidan HTTPS yoqing (`COOKIE_SECURE=1`), reverse proxy, rate limit, zaxira nusxa va Telegram xabar yuborish navbati sozlang. Hozirgi loyiha SQLite ishlatadi; katta savdo yuklamasi uchun PostgreSQL tavsiya qilinadi.
