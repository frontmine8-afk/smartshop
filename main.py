import os, json, sqlite3, hashlib, secrets, threading, time, urllib.request, urllib.parse, urllib.error, re, asyncio
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Depends, Header, UploadFile, File, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

BASE=Path(__file__).parent
DB=BASE/"smartshop.db"
UPLOADS=BASE/"uploads"; UPLOADS.mkdir(exist_ok=True)

def load_env():
    p=BASE/".env"
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line=line.strip()
            if line and not line.startswith("#") and "=" in line:
                k,v=line.split("=",1); os.environ.setdefault(k.strip(),v.strip().strip('"').strip("'"))
load_env()

def db():
    c=sqlite3.connect(DB, timeout=20); c.row_factory=sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON"); c.execute("PRAGMA busy_timeout=20000")
    return c

def pw_hash(pw, salt=None):
    salt=salt or secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac("sha256",pw.encode(),salt,310000)
    return salt.hex()+":"+digest.hex()

def pw_check(pw, stored):
    try:
        if ":" not in stored:
            return secrets.compare_digest(hashlib.sha256(pw.encode()).hexdigest(),stored)
        salt, digest=stored.split(":")
        return secrets.compare_digest(hashlib.pbkdf2_hmac("sha256",pw.encode(),bytes.fromhex(salt),310000).hex(),digest)
    except Exception: return False

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, username TEXT UNIQUE, password_hash TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,slug TEXT UNIQUE);
        CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,description TEXT DEFAULT '',price REAL NOT NULL DEFAULT 0,old_price REAL,cost_price REAL NOT NULL DEFAULT 0,stock INTEGER NOT NULL DEFAULT 0,category_id INTEGER,image TEXT DEFAULT '',images TEXT DEFAULT '[]',active INTEGER NOT NULL DEFAULT 1,created_at TEXT DEFAULT CURRENT_TIMESTAMP,FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT,customer_name TEXT NOT NULL,phone TEXT NOT NULL,region TEXT DEFAULT '',district TEXT DEFAULT '',address TEXT NOT NULL,location TEXT DEFAULT '',note TEXT DEFAULT '',items TEXT NOT NULL,total REAL NOT NULL,status TEXT DEFAULT 'new',created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,username TEXT NOT NULL,email TEXT NOT NULL UNIQUE,phone TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,phone_verified INTEGER NOT NULL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP,last_login TEXT);
        CREATE TABLE IF NOT EXISTS phone_links(phone TEXT PRIMARY KEY,chat_id TEXT NOT NULL,linked_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,amount REAL NOT NULL DEFAULT 0,created_at TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS otp_challenges(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,purpose TEXT NOT NULL,code_hash TEXT NOT NULL,expires_at INTEGER NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,blocked_until INTEGER NOT NULL DEFAULT 0,last_sent INTEGER NOT NULL,used INTEGER NOT NULL DEFAULT 0,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS user_sessions(token_hash TEXT PRIMARY KEY,user_id INTEGER NOT NULL,expires_at INTEGER NOT NULL,created_at INTEGER DEFAULT (strftime('%s','now')),FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS sms_events(id INTEGER PRIMARY KEY AUTOINCREMENT,phone_masked TEXT,purpose TEXT,success INTEGER,created_at INTEGER DEFAULT (strftime('%s','now')));
        CREATE TABLE IF NOT EXISTS telegram_accounts(id INTEGER PRIMARY KEY CHECK(id=1),phone TEXT,user_id TEXT,username TEXT,session_encrypted TEXT NOT NULL,connected_at TEXT DEFAULT CURRENT_TIMESTAMP);
        """)
        # Safe additive migrations for existing SMART SHOP databases.
        for table, col, decl in [
            ("products","cost_price","REAL NOT NULL DEFAULT 0"),
            ("users","surname","TEXT NOT NULL DEFAULT ''"),
            ("users","delivery_location","TEXT NOT NULL DEFAULT ''"),
            ("users","telegram_chat_id","TEXT NOT NULL DEFAULT ''"),
            ("orders","user_id","INTEGER"),
            ("orders","cost_total","REAL NOT NULL DEFAULT 0")]:
            cols=[r[1] for r in c.execute(f"PRAGMA table_info({table})")]
            if col not in cols: c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        if not c.execute("SELECT 1 FROM admins LIMIT 1").fetchone():
            c.execute("INSERT INTO admins(username,password_hash) VALUES(?,?)",(os.getenv("ADMIN_USERNAME","admin"),pw_hash(os.getenv("ADMIN_PASSWORD","ChangeThisPassword123!"))))
        defaults={"site_title":os.getenv("SITE_TITLE","SMART SHOP"),"favicon":"/static/favicon.svg","logo_text":"SMART SHOP","currency":"UZS","delivery_note":"Yetkazib berish narxi admin tomonidan aniqlanadi."}
        for k,v in defaults.items(): c.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",(k,v))
        if not c.execute("SELECT 1 FROM categories LIMIT 1").fetchone():
            for n,s in [("Elektronika","electronics"),("Aksessuarlar","accessories"),("Uy uchun","home"),("Boshqa","other")]: c.execute("INSERT INTO categories(name,slug) VALUES(?,?)",(n,s))
        if not c.execute("SELECT 1 FROM products LIMIT 1").fetchone():
            cat=c.execute("SELECT id FROM categories LIMIT 1").fetchone()["id"]
            samples=[
             ("Smart Watch S9","Aqlli soat",289000,349000,12,cat,"https://images.unsplash.com/photo-1523275335684-37898b6baf30?w=900"),
             ("Wireless Headphones","Simsiz quloqchin",199000,None,8,cat,"https://images.unsplash.com/photo-1505740420928-5e560c06d30e?w=900"),
             ("Minimal Backpack","Kundalik sumka",159000,189000,15,cat,"https://images.unsplash.com/photo-1553062407-98eeb64c6a62?w=900"),
             ("Desk Lamp","Stol chirog'i",119000,None,6,cat,"https://images.unsplash.com/photo-1507473885765-e6ed057f782c?w=900")]
            for x in samples: c.execute("INSERT INTO products(name,description,price,old_price,stock,category_id,image) VALUES(?,?,?,?,?,?,?)",x)

def settings_dict(c): return {r["key"]:r["value"] for r in c.execute("SELECT * FROM settings")}
def product_dict(r):
    d=dict(r)
    try:d["images"]=json.loads(d.get("images") or "[]")
    except:d["images"]=[]
    d["discount"]=round((1-d["price"]/d["old_price"])*100) if d.get("old_price") and d["old_price"]>d["price"] else 0
    return d

def bot_call(method,payload,token=None):
    token=(token or os.getenv("BOT_TOKEN","")).strip()
    if not token:return None
    req=urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",data=urllib.parse.urlencode(payload).encode())
    try:
        with urllib.request.urlopen(req,timeout=20) as r:return json.loads(r.read())
    except Exception as e: print("Telegram API error:",e); return None

def otp_send_telegram(phone, code):
    with db() as c:
        row=c.execute("SELECT chat_id FROM phone_links WHERE phone=?",(phone,)).fetchone()
    if not row:
        raise RuntimeError("Avval Telegram botga /start bosing va telefon kontaktingizni ulashing")
    result=bot_call("sendMessage",{"chat_id":row["chat_id"],"text":f"SMART SHOP tasdiqlash kodi: {code}\nKod 2 daqiqa amal qiladi. Uni hech kimga bermang."},token=os.getenv("OTP_BOT_TOKEN",""))
    if not result or not result.get("ok"):
        raise RuntimeError("Telegram orqali kod yuborilmadi. Botni ochib /start bosing va kontaktni qayta ulang")
    return True

def mask_phone(p): return p[:4]+"******"+p[-2:] if len(p)>7 else "***"
def otp_hash(user_id,purpose,code,salt):
    return hashlib.sha256(f"{user_id}:{purpose}:{code}:{salt}".encode()).hexdigest()

def issue_otp(user_id,phone,purpose):
    now=int(time.time())
    with db() as c:
        last=c.execute("SELECT last_sent FROM otp_challenges WHERE user_id=? AND purpose=? ORDER BY id DESC LIMIT 1",(user_id,purpose)).fetchone()
        if last and now-last["last_sent"]<60: raise HTTPException(429,"Qayta SMS yuborish uchun 60 soniya kuting")
        recent=c.execute("SELECT COUNT(*) n FROM sms_events WHERE phone_masked=? AND created_at>?",(mask_phone(phone),now-3600)).fetchone()["n"]
        if recent>=5: raise HTTPException(429,"SMS limiti tugadi. Keyinroq urinib ko'ring")
    code=f"{secrets.randbelow(1000000):06d}"; salt=secrets.token_hex(16)
    try: otp_send_telegram(phone,code)
    except Exception as e:
        with db() as c:c.execute("INSERT INTO sms_events(phone_masked,purpose,success) VALUES(?,?,0)",(mask_phone(phone),purpose))
        raise HTTPException(503,str(e))
    with db() as c:
        c.execute("INSERT INTO otp_challenges(user_id,purpose,code_hash,expires_at,last_sent) VALUES(?,?,?,?,?)",(user_id,purpose,otp_hash(user_id,purpose,code,salt)+":"+salt,now+120,now))
        c.execute("INSERT INTO sms_events(phone_masked,purpose,success) VALUES(?,?,1)",(mask_phone(phone),purpose))
    return {"sent":True,"phone":mask_phone(phone),"resend_after":60}

def verify_otp(user_id,purpose,code):
    now=int(time.time()); error=None
    c=db()
    try:
        row=c.execute("SELECT * FROM otp_challenges WHERE user_id=? AND purpose=? AND used=0 ORDER BY id DESC LIMIT 1",(user_id,purpose)).fetchone()
        if not row:error="Kod topilmadi. Yangi kod so'rang"
        elif row["blocked_until"]>now:error="Urinishlar vaqtincha bloklangan"
        elif row["expires_at"]<now:error="Kod muddati tugagan"
        else:
            stored,salt=row["code_hash"].split(":")
            if not secrets.compare_digest(stored,otp_hash(user_id,purpose,code,salt)):
                attempts=row["attempts"]+1;block=now+900 if attempts>=5 else 0
                c.execute("UPDATE otp_challenges SET attempts=?,blocked_until=? WHERE id=?",(attempts,block,row["id"]))
                error="Tasdiqlash kodi noto'g'ri"
            else:
                c.execute("UPDATE otp_challenges SET used=1 WHERE id=?",(row["id"],))
                if purpose=="register":c.execute("UPDATE users SET phone_verified=1 WHERE id=?",(user_id,))
                else:c.execute("UPDATE users SET last_login=CURRENT_TIMESTAMP WHERE id=?",(user_id,))
        c.commit()
    finally:c.close()
    if error:raise HTTPException(429 if "bloklangan" in error else 400,error)

def create_session(user_id,response):
    token=secrets.token_urlsafe(40); digest=hashlib.sha256(token.encode()).hexdigest()
    with db() as c:c.execute("INSERT INTO user_sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",(digest,user_id,int(time.time())+30*86400))
    response.set_cookie("shop_session",token,httponly=True,samesite="lax",secure=os.getenv("COOKIE_SECURE","0")=="1",max_age=30*86400,path="/")

def current_user(request:Request):
    token=request.cookies.get("shop_session")
    if not token: raise HTTPException(401,"Avval tizimga kiring")
    digest=hashlib.sha256(token.encode()).hexdigest()
    with db() as c:
        r=c.execute("SELECT u.* FROM user_sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?",(digest,int(time.time()))).fetchone()
        if not r or not r["phone_verified"]:raise HTTPException(401,"Telefon tasdiqlanmagan")
        return dict(r)

def admin_auth(authorization:str|None=Header(default=None)):
    token=(authorization or "").replace("Bearer ","")
    if not token or not secrets.compare_digest(token,os.environ.get("ADMIN_TOKEN","")):raise HTTPException(401,"Admin login talab qilinadi")
    return True

def send_order(order):
    ids=[x.strip() for x in os.getenv("ADMIN_CHAT_IDS","").split(",") if x.strip()]
    items=json.loads(order["items"])
    lines=[f"🛒 YANGI BUYURTMA №{order['id']}",f"👤 {order['customer_name']}",f"☎️ {order['phone']}",f"📍 {order['region']} / {order['district']}",f"Manzil: {order['address']}"]
    if order["location"]:lines.append("Lokatsiya: "+order["location"])
    if order["note"]:lines.append("Izoh: "+order["note"])
    lines.append("Mahsulotlar:")
    for x in items:lines.append(f"• {x['name']} × {x['qty']} — {x['price']*x['qty']:,.0f} so'm")
    lines.append(f"JAMI: {order['total']:,.0f} so'm")
    message="\n".join(lines)
    for chat in ids:
        bot_call("sendMessage",{"chat_id":chat,"text":message})
        for item in items:
            photo=item.get("image","")
            if photo.startswith("/"):
                photo=os.getenv("SITE_PUBLIC_URL","").rstrip("/")+photo
            if photo.startswith("http"):
                caption=f"{item['name']} × {item['qty']} — {item['price']*item['qty']:,.0f} so'm"
                bot_call("sendPhoto",{"chat_id":chat,"photo":photo,"caption":caption})
    try:
        with db() as c: acc=c.execute("SELECT session_encrypted FROM telegram_accounts WHERE id=1").fetchone()
        if acc:
            from telethon import TelegramClient
            from telethon.sessions import StringSession
            session=_fernet().decrypt(acc["session_encrypted"].encode()).decode()
            client=TelegramClient(StringSession(session),int(os.getenv("TELEGRAM_API_ID")),os.getenv("TELEGRAM_API_HASH"))
            async def send_saved():
                await client.connect()
                try: await client.send_message("me",message)
                finally: await client.disconnect()
            asyncio.run(send_saved())
    except Exception as e: print("Linked Telegram account notification error:",type(e).__name__)

BOT_STATES={}

def _telegram_account_configured():
    return bool(os.getenv("TELEGRAM_API_ID") and os.getenv("TELEGRAM_API_HASH") and os.getenv("SESSION_ENCRYPTION_KEY"))

def _fernet():
    from cryptography.fernet import Fernet
    key=os.getenv("SESSION_ENCRYPTION_KEY","").strip()
    if not key: raise RuntimeError("SESSION_ENCRYPTION_KEY sozlanmagan")
    return Fernet(key.encode())

def _run_telegram_account(action, **kwargs):
    from telethon import TelegramClient
    from telethon.sessions import StringSession
    from telethon.errors import SessionPasswordNeededError
    api_id=int(os.getenv("TELEGRAM_API_ID","0")); api_hash=os.getenv("TELEGRAM_API_HASH","")
    if not api_id or not api_hash: raise RuntimeError("TELEGRAM_API_ID va TELEGRAM_API_HASH kerak")
    client=TelegramClient(StringSession(kwargs.get("session_string", "")),api_id,api_hash)
    async def work():
        await client.connect()
        try:
            if action=="send_code":
                sent=await client.send_code_request(kwargs["phone"])
                return {"session":client.session.save(),"phone_code_hash":sent.phone_code_hash}
            if action=="verify_code":
                try:
                    await client.sign_in(phone=kwargs["phone"],code=kwargs["code"],phone_code_hash=kwargs["phone_code_hash"])
                except SessionPasswordNeededError:
                    return {"need_password":True,"session":client.session.save()}
            elif action=="verify_password":
                await client.sign_in(password=kwargs["password"])
            me=await client.get_me()
            return {"session":client.session.save(),"user_id":me.id,"username":me.username or "","phone":getattr(me,"phone","") or ""}
        finally:
            await client.disconnect()
    return asyncio.run(work())

def _save_linked_session(data):
    encrypted=_fernet().encrypt(data["session"].encode()).decode()
    with db() as c:
        c.execute("INSERT INTO telegram_accounts(id,phone,user_id,username,session_encrypted,connected_at) VALUES(1,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(id) DO UPDATE SET phone=excluded.phone,user_id=excluded.user_id,username=excluded.username,session_encrypted=excluded.session_encrypted,connected_at=CURRENT_TIMESTAMP",
          (data.get("phone",""),str(data["user_id"]),data.get("username",""),encrypted))

def bot_admin(chat):return str(chat) in [x.strip() for x in os.getenv("ADMIN_CHAT_IDS","").split(",") if x.strip()]
def bot_send(chat,text,markup=None):
    payload={"chat_id":chat,"text":text}
    if markup:payload["reply_markup"]=json.dumps(markup)
    bot_call("sendMessage",payload)
def bot_download_photo(file_id):
    token=os.getenv("BOT_TOKEN","")
    result=bot_call("getFile",{"file_id":file_id})
    if not result or not result.get("ok"):return ""
    path=result["result"]["file_path"]
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/file/bot{token}/{path}",timeout=30) as r:data=r.read()
        name=secrets.token_hex(12)+Path(path).suffix
        (UPLOADS/name).write_bytes(data)
        return "/uploads/"+name
    except Exception:return ""
def bot_update_product(state,photo_url=""):
    with db() as c:
        cur=c.execute("INSERT INTO products(name,description,price,old_price,stock,category_id,image,active) VALUES(?,?,?,?,?,?,?,1)",
          (state["name"],state.get("description",""),float(state["price"]),state.get("old_price"),int(state["stock"]),state.get("category_id"),photo_url))
        return cur.lastrowid
def telegram_poll():
    token=os.getenv("BOT_TOKEN","").strip()
    if not token:return
    offset=0
    while True:
        try:
            url=f"https://api.telegram.org/bot{token}/getUpdates?timeout=25&offset={offset}"
            with urllib.request.urlopen(url,timeout=35) as r:data=json.loads(r.read())
            for u in data.get("result",[]):
                offset=u["update_id"]+1; msg=u.get("message",{}); chat=msg.get("chat",{}).get("id")
                if not chat:continue
                txt=(msg.get("text") or "").strip()
                if txt.startswith("/start") or txt=="/myid":
                    if bot_admin(chat):
                        keyboard={"keyboard":[[{"text":"➕ Mahsulot qo‘shish"},{"text":"📦 Mahsulotlar"}],[{"text":"🔗 Telegram akkaunt ulash"},{"text":"ℹ️ Yordam"}]],"resize_keyboard":True}
                        bot_send(chat,f"SMART SHOP admin bot. Chat ID: {chat}",keyboard)
                    else:
                        keyboard={"keyboard":[[{"text":"📱 Telefon raqamimni ulash","request_contact":True}]],"resize_keyboard":True,"one_time_keyboard":True}
                        bot_send(chat,"SMART SHOP tasdiqlash kodlarini olish uchun pastdagi tugma orqali o‘z telefon kontaktingizni yuboring. Boshqa odamning raqamini yubormang.",keyboard)
                    continue
                if not bot_admin(chat):continue
                if txt=="/cancel":BOT_STATES.pop(str(chat),None);bot_send(chat,"Bekor qilindi.");continue
                if txt in ["ℹ️ Yordam","/help"]:
                    bot_send(chat,"/addproduct — mahsulot qo‘shish\n/products — mahsulotlar ro‘yxati\n/setstock ID SON — qoldiqni o‘zgartirish\n/deleteproduct ID — mahsulotni o‘chirish\n/connectaccount — Telegram akkaunt ulash\n/disconnectaccount — akkauntni uzish\n/accountstatus — ulanish holati\n/cancel — amalni bekor qilish")
                    continue
                if txt in ["➕ Mahsulot qo‘shish","/addproduct"]:
                    BOT_STATES[str(chat)]={"step":"name"};bot_send(chat,"Yangi mahsulot nomini yuboring. Bekor qilish: /cancel");continue
                if txt in ["📦 Mahsulotlar","/products"]:
                    with db() as c:rows=c.execute("SELECT id,name,price,stock FROM products ORDER BY id DESC LIMIT 30").fetchall()
                    bot_send(chat,"\n".join([f"#{r['id']} {r['name']} — {r['price']:,.0f} so'm | qoldiq: {r['stock']}" for r in rows]) or "Mahsulot yo'q");continue
                if txt.startswith("/deleteproduct "):
                    try:
                        pid=int(txt.split()[1])
                        with db() as c:c.execute("DELETE FROM products WHERE id=?",(pid,))
                        bot_send(chat,f"#{pid} o'chirildi.")
                    except:bot_send(chat,"Format: /deleteproduct ID")
                    continue
                if txt.startswith("/setstock "):
                    try:
                        _,pid,qty=txt.split()
                        with db() as c:c.execute("UPDATE products SET stock=? WHERE id=?",(max(0,int(qty)),int(pid)))
                        bot_send(chat,"Qoldiq yangilandi.")
                    except:bot_send(chat,"Format: /setstock ID SON")
                    continue
                if txt in ["🔗 Telegram akkaunt ulash","/connectaccount"]:
                    if not _telegram_account_configured():
                        bot_send(chat,"Telegram akkaunt ulash uchun .env faylida TELEGRAM_API_ID, TELEGRAM_API_HASH va SESSION_ENCRYPTION_KEY sozlang. API ma’lumotlari my.telegram.org saytidan olinadi.")
                    else:
                        BOT_STATES[str(chat)]={"step":"tg_phone"};bot_send(chat,"Ulamoqchi bo‘lgan Telegram akkauntingiz raqamini xalqaro formatda yuboring. Masalan: +998901234567\nLogin kodi va 2FA paroli hech qayerda logga yozilmaydi. Bu botni faqat o‘zingiz boshqaradigan maxfiy admin chatida ishlating.")
                    continue
                if txt=="/accountstatus":
                    with db() as c: acc=c.execute("SELECT phone,username,connected_at FROM telegram_accounts WHERE id=1").fetchone()
                    bot_send(chat,(f"Ulangan akkaunt: {acc['phone']} (@{acc['username'] or '—'})\nUlangan vaqt: {acc['connected_at']}") if acc else "Telegram akkaunt ulanmagan.");continue
                if txt=="/disconnectaccount":
                    with db() as c:c.execute("DELETE FROM telegram_accounts WHERE id=1")
                    bot_send(chat,"Ulangan Telegram akkaunt sessiyasi o‘chirildi.");continue
                state=BOT_STATES.get(str(chat))
                if not state:continue
                step=state["step"]
                if step=="tg_phone":
                    phone=re.sub(r"[^+0-9]","",txt)
                    if not re.fullmatch(r"\+[1-9]\d{7,14}",phone):bot_send(chat,"Telefon raqamini +998XXXXXXXXX formatida yuboring.");continue
                    try:
                        result=_run_telegram_account("send_code",phone=phone)
                        state.update({"step":"tg_code","phone":phone,"session":result["session"],"phone_code_hash":result["phone_code_hash"]})
                        bot_send(chat,"Telegram ilovasiga yuborilgan 5 xonali login kodini yuboring. Kodni hech kimga bermang.")
                    except Exception as e:bot_send(chat,"Kod yuborilmadi. API sozlamalari va telefon raqamini tekshiring: "+str(e)[:180])
                    continue
                if step=="tg_code":
                    if not re.fullmatch(r"\d{5,6}",txt):bot_send(chat,"Telegram yuborgan login kodini yuboring.");continue
                    if msg.get("message_id"):bot_call("deleteMessage",{"chat_id":chat,"message_id":msg["message_id"]})
                    try:
                        result=_run_telegram_account("verify_code",phone=state["phone"],code=txt,phone_code_hash=state["phone_code_hash"],session_string=state["session"])
                        if result.get("need_password"):
                            state.update({"step":"tg_password","session":result["session"]});bot_send(chat,"Telegram ikki bosqichli himoya parolini kiriting. U faqat xotirada vaqtincha ishlatiladi va saqlanmaydi.")
                        else:
                            _save_linked_session(result);BOT_STATES.pop(str(chat),None);bot_send(chat,"Telegram akkaunt muvaffaqiyatli ulandi. Xabarlar ushbu akkauntning Saved Messages bo‘limiga yuboriladi.")
                    except Exception as e:bot_send(chat,"Login kodi tasdiqlanmadi. Yangi urinish uchun /connectaccount: "+str(e)[:160]);BOT_STATES.pop(str(chat),None)
                    continue
                if step=="tg_password":
                    if msg.get("message_id"):bot_call("deleteMessage",{"chat_id":chat,"message_id":msg["message_id"]})
                    try:
                        result=_run_telegram_account("verify_password",password=txt,session_string=state["session"])
                        _save_linked_session(result);BOT_STATES.pop(str(chat),None);bot_send(chat,"Telegram akkaunt muvaffaqiyatli ulandi. 2FA paroli saqlanmadi. Xabarlar Saved Messages bo‘limiga yuboriladi.")
                    except Exception as e:bot_send(chat,"2FA paroli tasdiqlanmadi. "+str(e)[:160])
                    continue
                if step=="name" and txt:state["name"]=txt;state["step"]="price";bot_send(chat,"Narxini so'mda yuboring:")
                elif step=="price":
                    try:state["price"]=float(txt.replace(" ",""));state["step"]="old_price";bot_send(chat,"Eski narxni chegirma uchun yuboring yoki - yozing:")
                    except:bot_send(chat,"Narx faqat raqam bo'lsin.")
                elif step=="old_price":
                    try:state["old_price"]=None if txt=="-" else float(txt.replace(" ",""));state["step"]="stock";bot_send(chat,"Ombordagi sonini yuboring:")
                    except:bot_send(chat,"Eski narx raqam yoki - bo‘lishi kerak.")
                elif step=="stock":
                    try:state["stock"]=max(0,int(txt));state["step"]="description";bot_send(chat,"Tavsif yuboring (yoki -):")
                    except:bot_send(chat,"Qoldiq butun son bo'lsin.")
                elif step=="description":
                    state["description"]="" if txt=="-" else txt;state["step"]="photo";bot_send(chat,"Mahsulot rasmini yuboring. Rasm bo'lmasa /skip yozing.")
                elif step=="photo":
                    if txt=="/skip":pid=bot_update_product(state);BOT_STATES.pop(str(chat),None);bot_send(chat,f"Mahsulot qo'shildi. ID: {pid}")
                    elif msg.get("photo"):
                        url=bot_download_photo(msg["photo"][-1]["file_id"])
                        if not url:bot_send(chat,"Rasmni yuklab bo'lmadi. Qayta yuboring yoki /skip.")
                        else:pid=bot_update_product(state,url);BOT_STATES.pop(str(chat),None);bot_send(chat,f"Mahsulot rasm bilan qo'shildi. ID: {pid}")
                    else:bot_send(chat,"Rasm yuboring yoki /skip yozing.")
        except Exception as e:print("Telegram polling error:",e);time.sleep(4)

def otp_telegram_poll():
    token=os.getenv("OTP_BOT_TOKEN","").strip()
    if not token:return
    offset=0
    while True:
        try:
            url=f"https://api.telegram.org/bot{token}/getUpdates?timeout=25&offset={offset}"
            with urllib.request.urlopen(url,timeout=35) as r:data=json.loads(r.read())
            for upd in data.get("result",[]):
                offset=upd["update_id"]+1;msg=upd.get("message",{});chat=msg.get("chat",{}).get("id")
                if not chat:continue
                if (msg.get("text") or "").startswith("/start"):
                    markup={"keyboard":[[{"text":"📱 Telefon raqamimni ulash","request_contact":True}]],"resize_keyboard":True,"one_time_keyboard":True}
                    bot_call("sendMessage",{"chat_id":chat,"text":"SMART SHOP tasdiqlash botiga xush kelibsiz. Davom etish uchun o'zingizning Telegram telefon kontaktingizni yuboring.","reply_markup":json.dumps(markup)},token=token)
                contact=msg.get("contact")
                if contact:
                    sender=msg.get("from",{}).get("id")
                    if str(contact.get("user_id", ""))!=str(sender):
                        bot_call("sendMessage",{"chat_id":chat,"text":"Faqat o'zingizning kontaktingizni yuboring."},token=token);continue
                    phone=re.sub(r"[^0-9+]","",contact.get("phone_number",""))
                    if not phone.startswith("+"):phone="+"+phone
                    if re.fullmatch(r"\+998\d{9}",phone):
                        with db() as c:c.execute("INSERT INTO phone_links(phone,chat_id) VALUES(?,?) ON CONFLICT(phone) DO UPDATE SET chat_id=excluded.chat_id,linked_at=CURRENT_TIMESTAMP",(phone,str(chat)))
                        bot_call("sendMessage",{"chat_id":chat,"text":"Telefon raqamingiz bog'landi. Endi saytga qaytib tasdiqlash kodini so'rang."},token=token)
                    else:bot_call("sendMessage",{"chat_id":chat,"text":"Faqat +998 telefon raqami qo'llab-quvvatlanadi."},token=token)
        except Exception as e:print("OTP bot polling error:",type(e).__name__);time.sleep(4)

@asynccontextmanager
async def lifespan(app):
    init_db()
    if os.getenv("BOT_TOKEN","").strip():threading.Thread(target=telegram_poll,daemon=True).start()
    if os.getenv("OTP_BOT_TOKEN","").strip():threading.Thread(target=otp_telegram_poll,daemon=True).start()
    yield
app=FastAPI(title="SMART SHOP",lifespan=lifespan)
app.mount("/static",StaticFiles(directory=BASE/"static"),name="static")
app.mount("/uploads",StaticFiles(directory=UPLOADS),name="uploads")

class Register(BaseModel):
    username:str=Field(min_length=2,max_length=40)
    surname:str=Field(default="",max_length=60)
    email:str=Field(default="",max_length=160)
    phone:str
    password:str=Field(default="",max_length=128)
class Login(BaseModel):
    login:str
    password:str
class OTP(BaseModel):
    code:str=Field(pattern=r"^\d{6}$")
class ProductIn(BaseModel):
    name:str;description:str="";price:float=Field(ge=0);old_price:float|None=None;cost_price:float=Field(default=0,ge=0);stock:int=Field(ge=0);category_id:int|None=None;image:str="";active:bool=True
class CategoryIn(BaseModel):name:str
class OrderIn(BaseModel):
    customer_name:str;phone:str;region:str="";district:str="";address:str;location:str="";note:str="";items:list[dict]
class SettingIn(BaseModel):value:str

@app.get("/")
def home():return FileResponse(BASE/"static"/"index.html")
@app.get("/admin")
def admin_page():return FileResponse(BASE/"static"/"admin.html")
@app.post("/api/auth/register")
def register(x:Register,response:Response):
    phone=re.sub(r"[^\d+]","",x.phone)
    if not re.fullmatch(r"\+998\d{9}",phone):raise HTTPException(400,"Telefon +998XXXXXXXXX formatida bo'lsin")
    if x.email and ("@" not in x.email or "." not in x.email.split("@")[-1]):raise HTTPException(400,"Email noto'g'ri")
    with db() as c:
        existing=c.execute("SELECT * FROM users WHERE phone=?",(phone,)).fetchone()
        if existing and existing["phone_verified"]:
            raise HTTPException(409,"Bu telefon allaqachon ro‘yxatdan o‘tgan. Keyingi xarid uchun shu raqamdan foydalaning.")
        email=(x.email or f"{phone[1:]}@smartshop.local").lower()
        password=x.password or secrets.token_urlsafe(24)
        if existing:
            uid=existing["id"]
            c.execute("UPDATE users SET username=?,surname=?,email=?,password_hash=? WHERE id=?",(x.username,x.surname,email,pw_hash(password),uid))
        else:
            cur=c.execute("INSERT INTO users(username,surname,email,phone,password_hash) VALUES(?,?,?,?,?)",(x.username,x.surname,email,phone,pw_hash(password)))
            uid=cur.lastrowid
    result=issue_otp(uid,phone,"register")
    response.set_cookie("otp_register",str(uid),httponly=True,samesite="lax",secure=os.getenv("COOKIE_SECURE","0")=="1",max_age=600,path="/")
    return result
@app.post("/api/auth/register/verify")
def register_verify(x:OTP,response:Response,request:Request):
    uid=request.cookies.get("otp_register")
    if not uid: raise HTTPException(400,"Ro'yxatdan o'tish sessiyasi topilmadi")
    verify_otp(int(uid),"register",x.code)
    response.delete_cookie("otp_register")
    create_session(int(uid),response)
    return {"ok":True}
@app.post("/api/auth/register/resend")
def register_resend(x:dict,response:Response):
    phone=re.sub(r"[^\\d+]","",str(x.get("phone","")))
    email=str(x.get("email","")).lower()
    with db() as c:r=c.execute("SELECT id,phone FROM users WHERE email=? AND phone=? AND phone_verified=0",(email,phone)).fetchone()
    if not r:raise HTTPException(404,"Tasdiqlanmagan akkaunt topilmadi")
    result=issue_otp(r["id"],r["phone"],"register")
    response.set_cookie("otp_register",str(r["id"]),httponly=True,samesite="lax",secure=os.getenv("COOKIE_SECURE","0")=="1",max_age=600,path="/")
    return result
@app.post("/api/auth/phone-login")
def phone_login(x:dict,response:Response):
    phone=re.sub(r"[^\d+]","",str(x.get("phone","")))
    with db() as c:r=c.execute("SELECT id,phone FROM users WHERE phone=? AND phone_verified=1",(phone,)).fetchone()
    if not r:raise HTTPException(404,"Bu telefon ro‘yxatdan o‘tmagan. Yangi akkaunt yarating.")
    result=issue_otp(r["id"],r["phone"],"login")
    response.set_cookie("otp_login",str(r["id"]),httponly=True,samesite="lax",secure=os.getenv("COOKIE_SECURE","0")=="1",max_age=600,path="/")
    return result
@app.post("/api/auth/login")
def auth_login(x:Login,response:Response):
    with db() as c:r=c.execute("SELECT * FROM users WHERE email=? OR username=?",(x.login.lower(),x.login)).fetchone()
    if not r or not pw_check(x.password,r["password_hash"]):raise HTTPException(401,"Login yoki parol noto'g'ri")
    if not r["phone_verified"]:raise HTTPException(403,"Telefon tasdiqlanmagan. Ro'yxatdan o'tishni yakunlang.")
    result=issue_otp(r["id"],r["phone"],"login")
    response.set_cookie("otp_login",str(r["id"]),httponly=True,samesite="lax",secure=os.getenv("COOKIE_SECURE","0")=="1",max_age=600,path="/")
    return result
@app.post("/api/auth/login/resend")
def login_resend(request:Request):
    uid=request.cookies.get("otp_login")
    if not uid:raise HTTPException(400,"Login jarayoni topilmadi")
    with db() as c:r=c.execute("SELECT phone FROM users WHERE id=? AND phone_verified=1",(int(uid),)).fetchone()
    if not r:raise HTTPException(404,"Akkaunt topilmadi")
    return issue_otp(int(uid),r["phone"],"login")
@app.post("/api/auth/login/verify")
def login_verify(x:OTP,request:Request,response:Response):
    login_id=request.cookies.get("otp_login")
    if not login_id:raise HTTPException(400,"Login jarayoni topilmadi")
    uid=int(login_id);verify_otp(uid,"login",x.code);response.delete_cookie("otp_login")
    create_session(uid,response);return {"ok":True}
@app.get("/api/auth/me")
def me(user=Depends(current_user)):return {"id":user["id"],"username":user["username"],"surname":user.get("surname", ""),"email":user["email"],"phone":user["phone"],"delivery_location":user.get("delivery_location", "")}
@app.post("/api/auth/logout")
def logout(request:Request,response:Response):
    token=request.cookies.get("shop_session")
    if token:
        with db() as c:c.execute("DELETE FROM user_sessions WHERE token_hash=?",(hashlib.sha256(token.encode()).hexdigest(),))
    response.delete_cookie("shop_session");return {"ok":True}
@app.get("/api/public")
def public_data():
    with db() as c:return {"settings":settings_dict(c),"otp_bot_username":os.getenv("OTP_BOT_USERNAME", "").lstrip("@"),"categories":[dict(x) for x in c.execute("SELECT * FROM categories ORDER BY name")],"products":[product_dict(x) for x in c.execute("SELECT * FROM products WHERE active=1 ORDER BY id DESC")]}
@app.get("/api/products/{pid}")
def product_detail(pid:int):
    with db() as c:
        r=c.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
        if not r:raise HTTPException(404,"Mahsulot topilmadi")
        p=product_dict(r);sim=[product_dict(x) for x in c.execute("SELECT * FROM products WHERE active=1 AND id!=? AND category_id=? ORDER BY id DESC LIMIT 8",(pid,r["category_id"]))]
        if len(sim)<4:
            extra=[product_dict(x) for x in c.execute("SELECT * FROM products WHERE active=1 AND id!=? ORDER BY id DESC LIMIT 8",(pid,))]
            ids={x["id"] for x in sim};sim += [x for x in extra if x["id"] not in ids][:4-len(sim)]
        return {"product":p,"similar":sim}
@app.post("/api/orders")
def create_order(o:OrderIn,user=Depends(current_user)):
    if not o.customer_name.strip() or not o.phone.strip() or not o.address.strip() or not o.items:raise HTTPException(400,"Buyurtma ma'lumotlari to'liq emas")
    normalized=[];total=0
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        for item in o.items:
            try:pid=int(item["id"]);qty=int(item["qty"])
            except:raise HTTPException(400,"Savat ma'lumoti noto'g'ri")
            p=c.execute("SELECT * FROM products WHERE id=? AND active=1",(pid,)).fetchone()
            if qty<1 or qty>99 or not p or p["stock"]<qty:raise HTTPException(409,"Mahsulot mavjud emas yoki qoldiq yetarli emas")
            normalized.append({"id":pid,"name":p["name"],"price":p["price"],"cost_price":p["cost_price"],"qty":qty,"image":p["image"]});total+=p["price"]*qty
        cost_total=sum(x["cost_price"]*x["qty"] for x in normalized)
        cur=c.execute("INSERT INTO orders(customer_name,phone,region,district,address,location,note,items,total,user_id,cost_total) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(o.customer_name,o.phone,o.region,o.district,o.address,o.location,o.note,json.dumps(normalized,ensure_ascii=False),total,user["id"],cost_total))
        c.execute("UPDATE users SET username=?,delivery_location=? WHERE id=?",(o.customer_name,o.location,user["id"]))
        oid=cur.lastrowid
        for x in normalized:c.execute("UPDATE products SET stock=stock-? WHERE id=?",(x["qty"],x["id"]))
        order=dict(c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone())
    threading.Thread(target=send_order,args=(order,),daemon=True).start()
    return {"ok":True,"order_id":oid,"total":total}
@app.post("/api/admin/login")
def admin_login(x:dict):
    with db() as c:r=c.execute("SELECT * FROM admins WHERE username=?",(x.get("username"),)).fetchone()
    if not r or not pw_check(x.get("password",""),r["password_hash"]):raise HTTPException(401,"Login yoki parol noto'g'ri")
    if ":" not in r["password_hash"]:
        with db() as c:c.execute("UPDATE admins SET password_hash=? WHERE id=?",(pw_hash(x.get("password","")),r["id"]))
    token=secrets.token_urlsafe(32);os.environ["ADMIN_TOKEN"]=token;return {"token":token}
@app.get("/api/admin/overview")
def overview(_=Depends(admin_auth)):
    with db() as c:
        delivered=c.execute("SELECT COALESCE(SUM(total),0) n,COALESCE(SUM(cost_total),0) cost FROM orders WHERE status='delivered'").fetchone()
        sold=0
        for rr in c.execute("SELECT items FROM orders WHERE status='delivered'"):
            try:sold+=sum(int(i.get('qty',0)) for i in json.loads(rr['items']))
            except:pass
        expenses=c.execute("SELECT COALESCE(SUM(amount),0) n FROM expenses").fetchone()["n"]
        return {"orders":c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"],"revenue":delivered["n"],"cost":delivered["cost"],"profit":delivered["n"]-delivered["cost"]-expenses,"expenses":expenses,"sold":sold,"pending":c.execute("SELECT COUNT(*) n FROM orders WHERE status='new'").fetchone()["n"],"products":c.execute("SELECT COUNT(*) n FROM products").fetchone()["n"],"recent":[dict(x) for x in c.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 50")],"settings":settings_dict(c)}
@app.get("/api/admin/products")
def admin_products(_=Depends(admin_auth)):
    with db() as c:return [product_dict(x) for x in c.execute("SELECT * FROM products ORDER BY id DESC")]
@app.post("/api/admin/products")
def add_product(p:ProductIn,_=Depends(admin_auth)):
    with db() as c:
        cur=c.execute("INSERT INTO products(name,description,price,old_price,cost_price,stock,category_id,image,active) VALUES(?,?,?,?,?,?,?,?,?)",(p.name,p.description,p.price,p.old_price,p.cost_price,p.stock,p.category_id,p.image,int(p.active)))
        return {"ok":True,"id":cur.lastrowid}
@app.put("/api/admin/products/{pid}")
def edit_product(pid:int,p:ProductIn,_=Depends(admin_auth)):
    with db() as c:
        cur=c.execute("UPDATE products SET name=?,description=?,price=?,old_price=?,cost_price=?,stock=?,category_id=?,image=?,active=? WHERE id=?",(p.name,p.description,p.price,p.old_price,p.cost_price,p.stock,p.category_id,p.image,int(p.active),pid))
        if not cur.rowcount:raise HTTPException(404,"Mahsulot topilmadi")
        return {"ok":True}
@app.delete("/api/admin/products/{pid}")
def delete_product(pid:int,_=Depends(admin_auth)):
    with db() as c:c.execute("DELETE FROM products WHERE id=?",(pid,))
    return {"ok":True}
@app.get("/api/admin/categories")
def admin_categories(_=Depends(admin_auth)):
    with db() as c:return [dict(x) for x in c.execute("SELECT * FROM categories")]
@app.post("/api/admin/categories")
def add_category(x:CategoryIn,_=Depends(admin_auth)):
    slug="".join(ch.lower() if ch.isalnum() else "-" for ch in x.name).strip("-")
    with db() as c:
        try:cur=c.execute("INSERT INTO categories(name,slug) VALUES(?,?)",(x.name,slug))
        except sqlite3.IntegrityError:raise HTTPException(409,"Kategoriya mavjud")
        return {"id":cur.lastrowid}
@app.delete("/api/admin/categories/{cid}")
def del_category(cid:int,_=Depends(admin_auth)):
    with db() as c:c.execute("DELETE FROM categories WHERE id=?",(cid,))
    return {"ok":True}
@app.get("/api/admin/orders")
def admin_orders(_=Depends(admin_auth)):
    with db() as c:return [dict(x) for x in c.execute("SELECT * FROM orders ORDER BY id DESC")]
@app.patch("/api/admin/orders/{oid}")
def order_status(oid:int,status:str,_=Depends(admin_auth)):
    if status not in ["new","accepted","preparing","shipping","delivered","cancelled"]:raise HTTPException(400,"Noto'g'ri holat")
    with db() as c:
        c.execute("BEGIN IMMEDIATE")
        row=c.execute("SELECT * FROM orders WHERE id=?",(oid,)).fetchone()
        if not row:raise HTTPException(404,"Buyurtma topilmadi")
        order_items=json.loads(row["items"])
        if status=="cancelled" and row["status"]!="cancelled":
            for item in order_items:
                c.execute("UPDATE products SET stock=stock+? WHERE id=?",(int(item["qty"]),int(item["id"])))
        elif row["status"]=="cancelled" and status!="cancelled":
            for item in order_items:
                product=c.execute("SELECT stock FROM products WHERE id=?",(int(item["id"]),)).fetchone()
                if not product or product["stock"]<int(item["qty"]):raise HTTPException(409,"Omborda qayta faollashtirish uchun mahsulot yetarli emas")
            for item in order_items:
                c.execute("UPDATE products SET stock=stock-? WHERE id=?",(int(item["qty"]),int(item["id"])))
        c.execute("UPDATE orders SET status=? WHERE id=?",(status,oid))
    return {"ok":True}
@app.put("/api/admin/settings/{key}")
def update_setting(key:str,x:SettingIn,_=Depends(admin_auth)):
    if key not in ["site_title","favicon","logo_text","delivery_note"]:raise HTTPException(400,"Noto'g'ri sozlama")
    with db() as c:c.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,x.value))
    return {"ok":True}
@app.post("/api/admin/upload")
async def upload(file:UploadFile=File(...),_=Depends(admin_auth)):
    if file.content_type not in ["image/jpeg","image/png","image/webp","image/svg+xml","image/x-icon"]:raise HTTPException(400,"Rasm formati qo'llab-quvvatlanmaydi")
    ext=Path(file.filename or "image.png").suffix.lower() or ".png";name=secrets.token_hex(12)+ext;data=await file.read()
    if len(data)>5*1024*1024:raise HTTPException(413,"Rasm 5 MB dan kichik bo'lsin")
    (UPLOADS/name).write_bytes(data);return {"url":"/uploads/"+name}
@app.get("/api/admin/users")
def admin_users(_=Depends(admin_auth)):
    with db() as c:return [dict(x) for x in c.execute("SELECT id,username,email,phone,phone_verified,created_at,last_login FROM users ORDER BY id DESC")]
@app.get("/api/admin/sms-stats")
def sms_stats(_=Depends(admin_auth)):
    with db() as c:return {"total":c.execute("SELECT COUNT(*) n FROM sms_events").fetchone()["n"],"sent":c.execute("SELECT COUNT(*) n FROM sms_events WHERE success=1").fetchone()["n"],"failed":c.execute("SELECT COUNT(*) n FROM sms_events WHERE success=0").fetchone()["n"],"recent":[dict(x) for x in c.execute("SELECT phone_masked,purpose,success,created_at FROM sms_events ORDER BY id DESC LIMIT 100")]}
@app.patch("/api/admin/users/{uid}")
def admin_user_verify(uid:int,verified:bool,_=Depends(admin_auth)):
    with db() as c:
        cur=c.execute("UPDATE users SET phone_verified=? WHERE id=?",(int(verified),uid))
        if not cur.rowcount:raise HTTPException(404,"Foydalanuvchi topilmadi")
    return {"ok":True}
@app.get("/api/admin/expenses")
def get_expenses(_=Depends(admin_auth)):
    with db() as c:return [dict(x) for x in c.execute("SELECT * FROM expenses ORDER BY id DESC")]
@app.post("/api/admin/expenses")
def add_expense(x:dict,_=Depends(admin_auth)):
    title=str(x.get("title","")).strip();amount=float(x.get("amount",0))
    if not title or amount<=0:raise HTTPException(400,"Xarajat nomi va summasi kerak")
    with db() as c:cur=c.execute("INSERT INTO expenses(title,amount) VALUES(?,?)",(title,amount));return {"id":cur.lastrowid}
@app.delete("/api/admin/expenses/{eid}")
def delete_expense(eid:int,_=Depends(admin_auth)):
    with db() as c:c.execute("DELETE FROM expenses WHERE id=?",(eid,))
    return {"ok":True}
@app.get("/api/health")
def health():return {"ok":True}
