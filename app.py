import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests
from flask import Flask, request, render_template_string, redirect, url_for

# yoomoney (при необходимости)
try:
    from yoomoney import Quickpay, Client
except ImportError:
    Quickpay = None
    Client = None
    print("yoomoney не установлен! (pip install yoomoney)")

app = Flask(__name__)

############################
# НАСТРОЙКИ
############################
DB_NAME = "surfvpn.db"
FREE_TRIAL_DAYS = 7

OUTLINE_API_URL   = os.getenv('OUTLINE_API_URL', 'https://123.45.67.89:8080/API_SECRET')
OUTLINE_API_KEY   = os.getenv('OUTLINE_API_KEY', '4d18c537-566b-46c3-b937-bcc28378b306')
OUTLINE_DISABLE_SSL_CHECK = True

YOOMONEY_TOKEN    = os.getenv('YOOMONEY_TOKEN', '4100116412273743.9FF0D8315EF8D02914C839B78EAFF293DC40AF6FF2F0E0BB0B312E709C950E13462F1D21594AF6602C672CE7099E66EF89971092FE5721FD778ED82C94531CE214AF890905832DC355814DA3564B7F27C0F61AC402A9FBE0784E6DF116851ECDA2A8C1DA6BBE1B2B85E72BF04FBFBC61085747E5F662CF0406DB9CB4B36EF809')
YOOMONEY_RECEIVER = os.getenv('YOOMONEY_RECEIVER', '4100116412273743')

############################
# ИНИЦИАЛИЗАЦИЯ БД
############################
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

############################
# ПОДПИСКИ / ЛОГИКА
############################
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

def remove_subscription(user_id: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

############################
# OUTLINE API
############################
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
            j = resp.json()
            return j.get("accessUrl"), j.get("id")
        else:
            print(f"Create key error: {resp.status_code}, {resp.text}")
    except Exception as e:
        print(f"create_outline_key error: {e}")
    return None, None

def delete_outline_key(key_id: str) -> bool:
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
        print(f"delete_outline_key error: {e}")
        return False

############################
# ФОНОВЫЙ ПОТОК УДАЛЕНИЯ
############################
def subscription_checker():
    while True:
        try:
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT user_id, key_id, expiration FROM subscriptions")
            rows = c.fetchall()
            now = datetime.now()
            for user_id, key_id, exp_str in rows:
                if not exp_str:
                    continue
                try:
                    exp_dt = datetime.fromisoformat(exp_str)
                except:
                    continue
                if exp_dt < now:
                    ok = delete_outline_key(key_id)
                    if ok:
                        c.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
                        conn.commit()
                        print(f"Subscription {user_id} expired, key {key_id} removed.")
            conn.close()
        except Exception as e:
            print("subscription_checker error:", e)
        time.sleep(60)

threading.Thread(target=subscription_checker, daemon=True).start()

############################
# YOOMONEY
############################
def generate_payment_url(user_id: str, amount: float, description: str):
    if not Quickpay:
        print("yoomoney not installed, no link generated")
        return ""
    label = f"vpn_{user_id}_{uuid.uuid4().hex}"
    quickpay = Quickpay(
        receiver=YOOMONEY_RECEIVER,
        quickpay_form="shop",
        targets=description,
        paymentType="AC",
        sum=amount,
        label=label
    )
    return quickpay.base_url

############################
# 3-ЭКРАННЫЙ INTRO
############################
@app.route("/")
def index():
    return redirect("/intro?step=1")


INTRO1_HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Добро пожаловать (1/2)</title>
  <style>
    /* Увеличенные отступы, все элементы крупнее */
    html, body {
      margin: 0; padding: 0; width: 100%; height: 100%;
      background: #000; color: #fff;
      font-family: Arial, sans-serif;
      font-size: 120%; font-weight: bold;
    }
    .page {
      width: 100%; height: 100%;
      background: url('https://raw.githubusercontent.com/salihsukrov/mini-apps/ae346474722137a5f8244a54da2a034374ea09c3/1.jpeg')
        no-repeat center center / cover;
      position: relative;
    }
    .nav-button {
      position: absolute; bottom: 50px; left: 50%; transform: translateX(-50%);
      background: rgba(0,0,0,0.6);
      border: 3px solid #fff;
      border-radius: 12px;
      color: #fff; text-decoration: none;
      font-size: 1.4rem; padding: 15px 25px;
    }
    .nav-button:hover {
      background: rgba(255,255,255,0.3);
    }
  </style>
</head>
<body>
  <div class="page">
    <a href="/intro?step=2" class="nav-button">Далее</a>
  </div>
</body>
</html>
"""

INTRO2_HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Инструкция (2/2)</title>
  <style>
    html, body {
      margin: 0; padding: 0; width: 100%; height: 100%;
      background: #000; color: #fff;
      font-family: Arial, sans-serif;
      font-size: 120%; font-weight: bold;
    }
    .page {
      width: 100%; height: 100%;
      background: url('https://raw.githubusercontent.com/salihsukrov/mini-apps/ae346474722137a5f8244a54da2a034374ea09c3/2.jpeg')
        no-repeat center center / cover;
      position: relative;
    }
    .nav-button {
      position: absolute; bottom: 50px; left: 50%; transform: translateX(-50%);
      background: rgba(0,0,0,0.6);
      border: 3px solid #fff;
      border-radius: 12px;
      color: #fff; text-decoration: none;
      font-size: 1.4rem; padding: 15px 25px;
    }
    .nav-button:hover {
      background: rgba(255,255,255,0.3);
    }
  </style>
</head>
<body>
  <div class="page">
    <a href="/intro?step=3" class="nav-button">Далее</a>
  </div>
</body>
</html>
"""

@app.route("/intro")
def intro():
    step = request.args.get("step", "1")
    if step == "1":
        return INTRO1_HTML
    elif step == "2":
        return INTRO2_HTML
    else:
        # На 3м шаге идём в меню
        return redirect("/menu")

############################
# ГЛАВНОЕ МЕНЮ (3-я страница)
############################
MAIN_MENU_HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>SurfGuard - Главное меню</title>
  <style>
    html, body {
      margin:0; padding:0; background:#000; color:#800080;
      font-family:Arial, sans-serif; font-size:120%; font-weight:bold;
      width:100%; height:100%;
    }
    .container {
      max-width:1400px; margin:100px auto; padding:60px;
    }
    .sub-info {
      background:#222; border-radius:28px;
      padding:60px; margin-bottom:50px;
    }
    .sub-title {
      font-size:1.3rem; margin-bottom:10px;
    }
    .sub-remaining {
      font-size:1.6rem; margin-bottom:10px;
    }
    .sub-details {
      display:flex; gap:14px; margin-bottom:5px;
    }
    .sub-detail-box {
      background:#333; border-radius:10px;
      padding:10px 15px;
    }
    .menu-btn {
      display:block; width:100%;
      background:#333; color:#fff;
      text-align:left; padding:40px;
      margin:20px 0; border:none; border-radius:10px;
      font-size:1.2rem; cursor:pointer;
    }
    .menu-btn:hover {
      background:#444;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="sub-info">
      <div class="sub-title">PRO-подписка</div>
      <div class="sub-remaining">Осталось {{ days_left }} дней</div>
      <div class="sub-details">
        <div class="sub-detail-box">Статус: {{ status }}</div>
        <div class="sub-detail-box">Подписка: {{ sub_state }}</div>
      </div>
    </div>

    <button class="menu-btn" onclick="location.href='/partner';">
      💎 Бонусы
    </button>
    <button class="menu-btn" onclick="location.href='/instruction';">
      ⚙ Установка и настройка
    </button>
    <button class="menu-btn" onclick="location.href='/support';">
      ❓ Поддержка
    </button>
    <button class="menu-btn" onclick="location.href='/get_vpn';">
      🔥 Получить VPN
    </button>
  </div>
</body>
</html>
"""

@app.route("/menu")
def main_menu():
    user_id = "DEMO_USER"
    row = get_subscription(user_id)
    if row:
        outline_key, key_id, exp_str = row
        try:
            exp_dt = datetime.fromisoformat(exp_str)
            now = datetime.now()
            if exp_dt > now:
                diff = exp_dt - now
                days_left = diff.days
                status = "Оффлайн"  # Можно прописать «Онлайн»
                sub_state = "Активна"
            else:
                days_left = 0
                status = "Оффлайн"
                sub_state = "Неактивна"
        except:
            days_left = 0
            status = "Оффлайн"
            sub_state = "Ошибка даты"
    else:
        days_left = 0
        status = "Оффлайн"
        sub_state = "Неактивна"

    return render_template_string(MAIN_MENU_HTML,
        days_left=days_left,
        status=status,
        sub_state=sub_state
    )

############################
# ПОЛУЧИТЬ VPN
############################
GET_VPN_HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8">
  <title>Получить VPN</title>
  <style>
    html, body {
      margin:0; padding:0; background:#000; color:#fff;
      font-family:Arial, sans-serif; font-size:120%; font-weight:bold;
      width:100%; height:100%;
    }
    .container {
      max-width:600px; margin:50px auto; padding:30px;
    }
    .title {
      font-size:1.6rem; margin-bottom:20px;
    }
    .option {
      background:#333; border-radius:10px;
      padding:20px; margin:15px 0; cursor:pointer;
    }
    .option:hover {
      background:#444;
    }
    a {
      color:#fff; text-decoration:none;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="title">Продлить/Получить VPN</div>

    <div class="option">
      <a href="/free_trial?user_id=DEMO_USER">1 неделя (бесплатно)</a>
    </div>
    <div class="option">
      <a href="/pay?user_id=DEMO_USER&plan=1m">1 месяц (199₽)</a>
    </div>
    <div class="option">
      <a href="/pay?user_id=DEMO_USER&plan=3m">3 месяца (599₽)</a>
    </div>
    <div class="option">
      <a href="/pay?user_id=DEMO_USER&plan=6m">6 месяцев (1199₽)</a>
    </div>

    <p><a href="/menu" style="color:#fff;">← Назад</a></p>
  </div>
</body>
</html>
"""

@app.route("/get_vpn")
def get_vpn():
    return GET_VPN_HTML

############################
# МАРШРУТЫ /support /instruction /partner
############################
@app.route("/support")
def page_support():
    html = """
    <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
      <h2>Поддержка</h2>
      <p>Связаться: @SURFGUARD_VPN_help</p>
      <a href="/menu" style="color:#fff;">← Меню</a>
    </div>
    """
    return render_template_string(html)

@app.route("/instruction")
def page_instruction():
    html = """
    <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
      <h2>Инструкция</h2>
      <p>Шаги по настройке Outline и т.д.</p>
      <a href="/menu" style="color:#fff;">← Меню</a>
    </div>
    """
    return render_template_string(html)

@app.route("/partner")
def page_partner():
    html = """
    <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
      <h2>Партнёрская программа</h2>
      <p>Пригласите 5 друзей = +1 месяц VPN!</p>
      <a href="/menu" style="color:#fff;">← Меню</a>
    </div>
    """
    return render_template_string(html)

############################
# FREE_TRIAL / PAY
############################
@app.route("/free_trial")
def free_trial():
    user_id = request.args.get("user_id", "DEMO_USER")
    if is_free_trial_used(user_id):
        return render_template_string("""
        <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
          <h2>Бесплатная неделя</h2>
          <p>Вы уже использовали бесплатную неделю.</p>
          <a href="/menu" style="color:#fff;">← Меню</a>
        </div>
        """)
    key_name = f"{datetime.now():%Y-%m-%d %H:%M} - {user_id}"
    access_url, key_id = create_outline_key(key_name)
    if not access_url:
        return render_template_string("""
        <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
          <h2>Ошибка</h2>
          <p>Не удалось создать ключ.</p>
          <a href="/menu" style="color:#fff;">← Меню</a>
        </div>
        """)
    expiration = datetime.now() + timedelta(days=FREE_TRIAL_DAYS)
    set_free_trial_used(user_id)
    save_subscription(user_id, access_url, key_id, expiration)
    return render_template_string(f"""
    <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
      <h2>Бесплатная неделя активирована!</h2>
      <p>Ваш Outline key: <code>{access_url}</code></p>
      <p>Действует до {expiration:%Y-%m-%d %H:%M}</p>
      <a href="/menu" style="color:#fff;">← Меню</a>
    </div>
    """)

@app.route("/pay")
def pay():
    user_id = request.args.get("user_id", "DEMO_USER")
    plan = request.args.get("plan", "1m")

    if plan == "1m":
        amount = 199
        days = 30
        desc = "Оплата VPN (1 месяц)"
    elif plan == "3m":
        amount = 599
        days = 90
        desc = "Оплата VPN (3 месяца)"
    elif plan == "6m":
        amount = 1199
        days = 180
        desc = "Оплата VPN (6 месяцев)"
    else:
        return render_template_string("""
        <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
          <h2>Ошибка</h2>
          <p>Неверный план</p>
          <a href="/menu" style="color:#fff;">← Меню</a>
        </div>
        """)

    pay_url = generate_payment_url(user_id, amount, desc)
    if not pay_url:
        return render_template_string("""
        <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
          <h2>Ошибка оплаты</h2>
          <p>Не удалось сгенерировать ссылку.</p>
          <a href="/menu" style="color:#fff;">← Меню</a>
        </div>
        """)

    return render_template_string(f"""
    <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
      <h2>{desc} ({amount}₽)</h2>
      <p><a href="{pay_url}" target="_blank" style="color:#fff;">Оплатить</a></p>
      <p>После оплаты <a href="/after_payment?user_id={user_id}&days={days}" style="color:#fff;">нажмите сюда</a>, чтобы активировать доступ.</p>
      <a href="/menu" style="color:#fff;">← Меню</a>
    </div>
    """)

@app.route("/after_payment")
def after_payment():
    user_id = request.args.get("user_id", "DEMO_USER")
    days_str = request.args.get("days", "30")
    try:
        days = int(days_str)
    except:
        days = 30

    key_name = f"{datetime.now():%Y-%m-%d %H:%M} - {user_id}"
    access_url, key_id = create_outline_key(key_name)
    if not access_url:
        return render_template_string("""
        <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
          <h2>Ошибка</h2>
          <p>Не удалось создать ключ!</p>
          <a href="/menu" style="color:#fff;">← Меню</a>
        </div>
        """)

    expiration = datetime.now() + timedelta(days=days)
    save_subscription(user_id, access_url, key_id, expiration)

    return render_template_string(f"""
    <div style="max-width:800px; margin:40px auto; background:#222; padding:40px; border-radius:10px; color:#fff; font-size:120%; font-weight:bold;">
      <h2>Платёж подтверждён</h2>
      <p>Подписка действует до {expiration:%Y-%m-%d %H:%M}.</p>
      <p>Outline key: <code>{access_url}</code></p>
      <a href="/menu" style="color:#fff;">← Меню</a>
    </div>
    """)

########################
# ЗАПУСК
########################
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 8080)), debug=False)
