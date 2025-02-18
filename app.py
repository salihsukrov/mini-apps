import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests
from flask import Flask, request, render_template_string, redirect, url_for

# Если нужно YooMoney:
try:
    from yoomoney import Quickpay, Client
except ImportError:
    Quickpay = None
    Client = None
    print("yoomoney не установлен! (pip install yoomoney)")

app = Flask(__name__)

##########################
# НАСТРОЙКИ
##########################
DB_NAME = "surfvpn.db"
FREE_TRIAL_DAYS = 7

OUTLINE_API_URL   = os.getenv('OUTLINE_API_URL', 'https://194.87.83.100:12245/ys7r0QWOtNdWJGUDtAvqGw')
OUTLINE_API_KEY   = os.getenv('OUTLINE_API_KEY', '4d18c537-566b-46c3-b937-bcc28378b306')
OUTLINE_DISABLE_SSL_CHECK = True

YOOMONEY_TOKEN    = os.getenv('YOOMONEY_TOKEN', '4100116412273743.9FF0D8315EF8D02914C839B78EAFF293DC40AF6FF2F0E0BB0B312E709C950E13462F1D21594AF6602C672CE7099E66EF89971092FE5721FD778ED82C94531CE214AF890905832DC355814DA3564B7F27C0F61AC402A9FBE0784E6DF116851ECDA2A8C1DA6BBE1B2B85E72BF04FBFBC61085747E5F662CF0406DB9CB4B36EF809')
YOOMONEY_RECEIVER = os.getenv('YOOMONEY_RECEIVER', '4100116412273743')

##########################
# ССЫЛКИ НА ФОНЫ / ИЗОБРАЖЕНИЯ (RAW)
##########################
# Для intro‑страниц используем прямые ссылки (Imgur дает прямые ссылки вида https://i.imgur.com/XXXXXXX.jpg)
INTRO1_IMG = "https://i.imgur.com/CsMwcj5.jpg"   # вместо "https://imgur.com/CsMwcj5"
INTRO2_IMG = "https://i.imgur.com/UA3ppM3.jpg"   # вместо "https://imgur.com/UA3ppM3"

# Остальные фоновые изображения – убедитесь, что они возвращают изображение напрямую.
MAIN_MENU_BG   = "https://github.com/salihsukrov/mini-apps/blob/main/4.jpg?raw=true"
PARTNER_BG     = "https://github.com/salihsukrov/mini-apps/blob/main/4.jpg?raw=true"
GETVPN_BG      = "https://github.com/salihsukrov/mini-apps/blob/main/4.jpg?raw=true"
INSTRUCTION_BG = "https://github.com/salihsukrov/mini-apps/blob/main/4.jpg?raw=true"

TELEGRAM_CHANNEL_LINK = "https://t.me/YourChannelHere"

##########################
# ИНИЦИАЛИЗАЦИЯ БД
##########################
def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id TEXT PRIMARY KEY,
            free_trial_used INTEGER DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            user_id TEXT PRIMARY KEY,
            outline_key TEXT,
            key_id TEXT,
            expiration TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS referrals (
            referrer_id TEXT,
            referral_id TEXT,
            display_name TEXT,
            PRIMARY KEY(referrer_id, referral_id)
        )
    """)
    conn.commit()
    conn.close()

init_db()

def get_conn():
    return sqlite3.connect(DB_NAME, check_same_thread=False)

##########################
# ПОДПИСКИ / ЛОГИКА
##########################
def is_free_trial_used(user_id: str) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT free_trial_used FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return bool(row[0]) if row else False

def set_free_trial_used(user_id: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, free_trial_used)
        VALUES (?, 1)
        ON CONFLICT(user_id) DO UPDATE SET free_trial_used=1
    """, (user_id,))
    conn.commit()
    conn.close()

def save_subscription(user_id: str, outline_key: str, key_id: str, expiration: datetime):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO subscriptions (user_id, outline_key, key_id, expiration)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET outline_key=?, key_id=?, expiration=?
    """, (user_id, outline_key, key_id, expiration.isoformat(),
          outline_key, key_id, expiration.isoformat()))
    conn.commit()
    conn.close()

def get_subscription(user_id: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT outline_key, key_id, expiration FROM subscriptions WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row

##########################
# OUTLINE API
##########################
def create_outline_key(name: str):
    headers = {"Content-Type": "application/json"}
    if OUTLINE_API_KEY:
        headers["Authorization"] = f"Bearer {OUTLINE_API_KEY}"
    verify_ssl = not OUTLINE_DISABLE_SSL_CHECK
    try:
        resp = requests.post(
            f"{OUTLINE_API_URL}/access-keys",
            json={"name": name},
            headers=headers,
            verify=verify_ssl,
            timeout=10
        )
        if resp.status_code in (200, 201):
            data = resp.json()
            return data.get("accessUrl"), data.get("id")
        else:
            print("Ошибка Outline:", resp.status_code, resp.text)
    except Exception as e:
        print("Ошибка создания Outline ключа:", e)
    return None, None

def delete_outline_key(key_id: str):
    if not key_id:
        return False
    headers = {}
    if OUTLINE_API_KEY:
        headers["Authorization"] = f"Bearer {OUTLINE_API_KEY}"
    verify_ssl = not OUTLINE_DISABLE_SSL_CHECK
    try:
        resp = requests.delete(
            f"{OUTLINE_API_URL}/access-keys/{key_id}",
            headers=headers,
            verify=verify_ssl,
            timeout=10
        )
        return resp.status_code in (200, 204)
    except Exception as e:
        print("Ошибка удаления Outline ключа:", e)
        return False

##########################
# ФОНОВЫЙ ПОТОК (удаление просроченных подписок)
##########################
def subscription_checker():
    while True:
        try:
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT user_id, key_id, expiration FROM subscriptions")
            rows = c.fetchall()
            now = datetime.now()
            for user_id, kid, exp_str in rows:
                if not exp_str:
                    continue
                try:
                    dt = datetime.fromisoformat(exp_str)
                except:
                    continue
                if dt < now:
                    ok = delete_outline_key(kid)
                    if ok:
                        c.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
                        conn.commit()
                        print(f"Удалена просроченная подписка: {user_id}, ключ={kid}")
            conn.close()
        except Exception as e:
            print("checker error:", e)
        time.sleep(60)

threading.Thread(target=subscription_checker, daemon=True).start()

##########################
# YOOMONEY
##########################
def generate_payment_url(user_id: str, amount: float, description: str):
    if not Quickpay:
        print("YooMoney не установлен => ссылка не сгенерирована")
        return ""
    label = f"vpn_{user_id}_{uuid.uuid4().hex}"
    quick = Quickpay(
        receiver=YOOMONEY_RECEIVER,
        quickpay_form="shop",
        targets=description,
        paymentType="AC",
        sum=amount,
        label=label
    )
    return quick.base_url

##########################
# ДВЕ СТРАНИЦЫ INTRO
##########################
INTRO1_HTML = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Intro 1/2</title>
  <style>
    body {{
      margin:0; padding:0;
      background:#000; color:#fff;
      font-family:Arial, sans-serif; font-size:120%; font-weight:bold;
      width:100%; height:100%;
    }}
    .page {{
      width:100%; height:100%;
      background: url('{INTRO1_IMG}') no-repeat center center / cover;
      position: relative;
    }}
    .nav-button {{
      position: absolute; bottom: 50px; left: 50%; transform: translateX(-50%);
      background: rgba(0,0,0,0.6);
      border: 3px solid #fff; border-radius: 12px;
      color: #fff; text-decoration: none;
      font-size: 1.4rem; padding: 15px 25px;
    }}
    .nav-button:hover {{
      background: rgba(255,255,255,0.3);
    }}
  </style>
</head>
<body>
  <div class="page">
    <a href="/intro?step=2" class="nav-button">Далее</a>
  </div>
</body>
</html>
"""

INTRO2_HTML = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Intro 2/2</title>
  <style>
    body {{
      margin:0; padding:0;
      background:#000; color:#fff;
      font-family:Arial, sans-serif; font-size:120%; font-weight:bold;
      width:100%; height:100%;
    }}
    .page {{
      width:100%; height:100%;
      background: url('{INTRO2_IMG}') no-repeat center center / cover;
      position: relative;
    }}
    .nav-button {{
      position: absolute; bottom: 50px; left: 50%; transform: translateX(-50%);
      background: rgba(0,0,0,0.6);
      border: 3px solid #fff; border-radius: 12px;
      color: #fff; text-decoration: none;
      font-size: 1.4rem; padding: 15px 25px;
    }}
    .nav-button:hover {{
      background: rgba(255,255,255,0.3);
    }}
  </style>
</head>
<body>
  <div class="page">
    <a href="/menu" class="nav-button">Далее</a>
  </div>
</body>
</html>
"""

@app.route("/")
def index():
    return redirect("/intro?step=1")

@app.route("/intro")
def intro():
    step = request.args.get("step", "1")
    if step == "1":
        return INTRO1_HTML
    elif step == "2":
        return INTRO2_HTML
    else:
        return redirect("/menu")

##########################
# ГЛАВНОЕ МЕНЮ
##########################
MAIN_MENU_PAGE = """
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Главное Меню</title>
  <style>
    body {
      margin:0; padding:0;
      background: url('{bg}') no-repeat center center / cover;
      font-family:Arial, sans-serif; color:#fff; font-size:120%; font-weight:bold;
      min-height:100vh;
    }
    .overlay {
      background: rgba(0,0,0,0.5);
      min-height: 100vh;
      padding: 40px 20px;
    }
    .container {
      max-width: 700px;
      margin: 0 auto;
    }
    .sub-info {
      background: #222;
      border-radius: 14px;
      padding: 30px;
      margin-bottom: 25px;
    }
    .sub-title {
      font-size: 1.3rem;
      margin-bottom: 10px;
    }
    .sub-remaining {
      font-size: 1.6rem;
      margin-bottom: 10px;
    }
    .sub-details {
      display: flex;
      gap: 14px;
      margin-bottom: 5px;
    }
    .sub-detail-box {
      background: #333;
      border-radius: 10px;
      padding: 10px 15px;
    }
    .menu-btn {
      display: block;
      width: 100%;
      background: #333;
      color: #fff;
      text-align: left;
      padding: 20px;
      margin: 10px 0;
      border: none;
      border-radius: 10px;
      font-size: 1.2rem;
      cursor: pointer;
    }
    .menu-btn:hover {
      background: #444;
    }
  </style>
</head>
<body>
  <div class="overlay">
    <div class="container">
      <div class="sub-info">
        <div class="sub-title">PRO-подписка</div>
        <div class="sub-remaining">Осталось {days_left} дней</div>
        <div class="sub-details">
          <div class="sub-detail-box">Статус: {status}</div>
          <div class="sub-detail-box">Подписка: {sub_state}</div>
        </div>
      </div>
      <button class="menu-btn" onclick="location.href='/instruction'">
        ⚙ Быстрая настройка
      </button>
      <button class="menu-btn" onclick="location.href='/partner'">
        💎 Бонусы
      </button>
      <button class="menu-btn" onclick="location.href='/support'">
        ❓ Поддержка
      </button>
      <button class="menu-btn" onclick="location.href='/get_vpn'">
        🔥 Получить VPN
      </button>
    </div>
  </div>
</body>
</html>
"""

@app.route("/menu")
def menu():
    user_id = "DEMO_USER"
    row = get_subscription(user_id)
    if row:
        outline_key, key_id, exp_str = row
        try:
            dt = datetime.fromisoformat(exp_str)
            now = datetime.now()
            if dt > now:
                diff = dt - now
                days_left = diff.days
                status = "Оффлайн"
                sub_state = "Активна"
            else:
                days_left = 0
                status = "Оффлайн"
                sub_state = "Неактивна"
        except:
            days_left = 0
            status = "Оффлайн"
            sub_state = "Ошибка"
    else:
        days_left = 0
        status = "Оффлайн"
        sub_state = "Неактивна"
    return MAIN_MENU_PAGE.format(
        bg=MAIN_MENU_BG,
        days_left=days_left,
        status=status,
        sub_state=sub_state
    )

##########################
# ИНСТРУКЦИЯ
##########################
INSTRUCTION_PAGE = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Быстрая настройка</title>
  <style>
    body {{
      margin:0; padding:0;
      background: url('{INSTRUCTION_BG}') no-repeat center center / cover;
      font-family:Arial, sans-serif; color:#fff; font-size:120%; font-weight:bold;
      min-height:100vh;
      display: flex; flex-direction: column; justify-content: center; align-items: center;
    }}
    .overlay {{
      background: rgba(0,0,0,0.5);
      width: 100%; min-height: 100vh;
      display: flex; flex-direction: column; justify-content: center; align-items: center;
    }}
    .icon {{
      font-size: 4rem; margin-bottom: 20px;
    }}
    h1 {{
      margin-bottom: 10px; font-size: 2rem;
    }}
    p.desc {{
      max-width: 500px; text-align: center; margin-bottom: 30px;
      font-weight: normal; line-height: 1.4; font-size: 1rem;
    }}
    .btn-start {{
      background: #fff; color: #000; font-size: 1.2rem;
      padding: 15px 30px; border-radius: 30px; border: none; cursor: pointer;
    }}
    .btn-start:hover {{
      background: #eee;
    }}
  </style>
</head>
<body>
  <div class="overlay">
    <div class="icon">🛠</div>
    <h1>Быстрая настройка</h1>
    <p class="desc">Первичная настройка для запуска VPN</p>
    <button class="btn-start" onclick="location.href='{TELEGRAM_CHANNEL_LINK}'">
      Начать
    </button>
  </div>
</body>
</html>
"""

@app.route("/instruction")
def instruction():
    return INSTRUCTION_PAGE

##########################
# ПАРТНЕРКА
##########################
PARTNER_PAGE = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Партнёрская программа</title>
  <style>
    body {{
      margin:0; padding:0;
      background: url('{PARTNER_BG}') no-repeat center center / cover;
      font-family:Arial, sans-serif; color:#fff; font-size:120%; font-weight:bold;
      min-height:100vh;
    }}
    .overlay {{
      background: rgba(0,0,0,0.6);
      min-height: 100vh; padding: 40px;
    }}
    .content {{
      max-width: 700px; margin: 0 auto;
      background: rgba(255,255,255,0.1); border-radius: 10px;
      padding: 30px;
    }}
    h2 {{
      margin-top: 0;
    }}
    a {{
      color: #fff; text-decoration: none;
    }}
  </style>
</head>
<body>
  <div class="overlay">
    <div class="content">
      <h2>Партнёрская программа</h2>
      <p>Приглашайте друзей и получайте бонусы</p>
      <a href="/menu">← Меню</a>
    </div>
  </div>
</body>
</html>
"""

@app.route("/partner")
def partner():
    return PARTNER_PAGE

##########################
# «Получить VPN»
##########################
GETVPN_PAGE = f"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Получить VPN</title>
  <style>
    body {{
      margin:0; padding:0;
      background: url('{GETVPN_BG}') no-repeat center center / cover;
      font-family:Arial, sans-serif; color:#fff; font-size:120%; font-weight:bold;
      min-height:100vh;
    }}
    .overlay {{
      background: rgba(0,0,0,0.6);
      min-height: 100vh; padding: 40px;
    }}
    .container {{
      max-width: 600px; margin: 0 auto;
    }}
    h2 {{
      margin-top: 0; margin-bottom: 20px;
      font-size: 1.6rem;
    }}
    .option {{
      background: #333; border-radius: 10px;
      padding: 20px; margin: 15px 0; cursor: pointer;
    }}
    .option:hover {{
      background: #444;
    }}
    a {{
      color: #fff; text-decoration: none;
    }}
  </style>
</head>
<body>
  <div class="overlay">
    <div class="container">
      <h2>Продлить/Получить VPN</h2>
      <div class="option"><a href="/free_trial?user_id=DEMO_USER">1 неделя (бесплатно)</a></div>
      <div class="option"><a href="/pay?user_id=DEMO_USER&plan=1m">1 месяц (199₽)</a></div>
      <div class="option"><a href="/pay?user_id=DEMO_USER&plan=3m">3 месяца (599₽)</a></div>
      <div class="option"><a href="/pay?user_id=DEMO_USER&plan=6m">6 месяцев (1199₽)</a></div>
      <p><a href="/menu" style="color:#fff;">← Назад</a></p>
    </div>
  </div>
</body>
</html>
"""

@app.route("/get_vpn")
def get_vpn():
    return GETVPN_PAGE

##########################
# SUPPORT
##########################
@app.route("/support")
def support():
    html = """
    <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
      <h2>Поддержка</h2>
      <p>Пишите: @SURFGUARD_VPN_help</p>
      <a href="/menu" style="color:#fff;">← Меню</a>
    </div>
    """
    return html

##########################
# FREE TRIAL / PAY
##########################
@app.route("/free_trial")
def free_trial():
    user_id = request.args.get("user_id", "DEMO_USER")
    if is_free_trial_used(user_id):
        return """
        <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
          <h2>Бесплатная неделя</h2>
          <p>Вы уже использовали бесплатную неделю.</p>
          <a href="/menu" style="color:#fff;">← Меню</a>
        </div>
        """
    key_name = f"{datetime.now():%Y-%m-%d %H:%M} - {user_id}"
    access_url, kid = create_outline_key(key_name)
    if not access_url:
        return """
        <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
          <h2>Ошибка</h2>
          <p>Не удалось создать ключ.</p>
          <a href="/menu" style="color:#fff;">← Меню</a>
        </div>
        """
    expiration = datetime.now() + timedelta(days=FREE_TRIAL_DAYS)
    set_free_trial_used(user_id)
    save_subscription(user_id, access_url, kid, expiration)
    return f"""
    <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
      <h2>Бесплатная неделя активирована!</h2>
      <p>Ваш Outline key: <code>{access_url}</code></p>
      <p>Действует до {expiration:%Y-%m-%d %H:%M}</p>
      <a href="/menu" style="color:#fff;">← Меню</a>
    </div>
    """

@app.route("/pay")
def pay():
    user_id = request.args.get("user_id", "DEMO_USER")
    plan = request.args.get("plan", "1m")
    if plan == "1m":
        amount = 199; days = 30; desc = "1 месяц (199₽)"
    elif plan == "3m":
        amount = 599; days = 90; desc = "3 месяца (599₽)"
    elif plan == "6m":
        amount = 1199; days = 180; desc = "6 месяцев (1199₽)"
    else:
        return """
        <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
          <h2>Ошибка</h2>
          <p>Неверный план</p>
          <a href="/menu" style="color:#fff;">← Меню</a>
        </div>
        """
    pay_url = generate_payment_url(user_id, amount, desc)
    if not pay_url:
        return """
        <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
          <h2>Ошибка оплаты</h2>
          <p>Не удалось сгенерировать ссылку.</p>
          <a href="/menu" style="color:#fff;">← Меню</a>
        </div>
        """
    return f"""
    <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
      <h2>{desc}</h2>
      <p><a href="{pay_url}" target="_blank" style="color:#fff;">Оплатить</a></p>
      <p>После оплаты <a href="/after_payment?user_id={user_id}&days={days}" style="color:#fff;">нажмите здесь</a> для активации</p>
      <a href="/menu" style="color:#fff;">← Меню</a>
    </div>
    """

@app.route("/after_payment")
def after_payment():
    user_id = request.args.get("user_id", "DEMO_USER")
    days_str = request.args.get("days", "30")
    try:
        days = int(days_str)
    except:
        days = 30
    key_name = f"{datetime.now():%Y-%m-%d %H:%M} - {user_id}"
    access_url, kid = create_outline_key(key_name)
    if not access_url:
        return """
        <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
          <h2>Ошибка</h2>
          <p>Не удалось создать ключ!</p>
          <a href="/menu" style="color:#fff;">← Меню</a>
        </div>
        """
    expiration = datetime.now() + timedelta(days=days)
    save_subscription(user_id, access_url, kid, expiration)
    return f"""
    <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
      <h2>Платёж подтверждён</h2>
      <p>Подписка действует до {expiration:%Y-%m-%d %H:%M}.</p>
      <p>Ваш Outline key: <code>{access_url}</code></p>
      <a href="/menu" style="color:#fff;">← Меню</a>
    </div>
    """

##########################
# ЗАПУСК
##########################
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8080")), debug=False)
