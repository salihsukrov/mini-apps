import os
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests
from flask import Flask, request, render_template_string, redirect, url_for, jsonify

# Если нужна библиотека yoomoney, убедитесь, что она установлена:
# pip install yoomoney
try:
    from yoomoney import Quickpay, Client
except ImportError:
    Quickpay = None
    Client = None
    print("yoomoney не установлен! Установите его через pip install yoomoney")

# -----------------------------
#  НАСТРОЙКИ / КОНСТАНТЫ
# -----------------------------
API_TOKEN = "TELEGRAM_BOT_TOKEN_IGNORED"  # здесь уже не используется
YOOMONEY_TOKEN    = os.getenv('YOOMONEY_TOKEN', '4100116412273743.9FF0D8315EF8D02914C839B78EAFF293DC40AF6FF2F0E0BB0B312E709C950E13462F1D21594AF6602C672CE7099E66EF89971092FE5721FD778ED82C94531CE214AF890905832DC355814DA3564B7F27C0F61AC402A9FBE0784E6DF116851ECDA2A8C1DA6BBE1B2B85E72BF04FBFBC61085747E5F662CF0406DB9CB4B36EF809')
YOOMONEY_RECEIVER = os.getenv('YOOMONEY_RECEIVER', '4100116412273743')

# Outline API: 
OUTLINE_API_URL   = os.getenv('OUTLINE_API_URL', 'https://194.87.83.100:12245/ys7r0QWOtNdWJGUDtAvqGw')
OUTLINE_API_KEY   = os.getenv('OUTLINE_API_KEY', '4d18c537-566b-46c3-b937-bcc28378b306')  # Bearer-токен, если нужно
OUTLINE_DISABLE_SSL_CHECK = True  # Иногда надо отключать проверку SSL (небезопасно!)

DB_NAME = "surfvpn.db"
FREE_TRIAL_DAYS = 7

# Картинка фона (в стиле киберпанк) — замените на желаемую
BG_IMAGE_URL = "https://github.com/salihsukrov/mini-apps/blob/60fbefe35116225d286b4a32d6cd8d60a8df6503/backgro.jpg"

app = Flask(__name__)

# -----------------------------
#  СОЗДАНИЕ / ИНИЦИАЛИЗАЦИЯ БД
# -----------------------------
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

# -----------------------------
#  УТИЛИТЫ ДЛЯ ПОДПИСОК, РЕФЕРАЛОВ, БД
# -----------------------------
def is_free_trial_used(user_id: str) -> bool:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT free_trial_used FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    return bool(row[0])

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

def add_referral(referrer_id: str, referral_id: str, display_name: str):
    conn = get_conn()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO referrals (referrer_id, referral_id, display_name)
            VALUES (?, ?, ?)
        """, (referrer_id, referral_id, display_name))
        conn.commit()
    except Exception as e:
        print(f"Error adding referral: {e}")
    conn.close()

def get_referral_count(referrer_id: str) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (referrer_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def get_referrals_list(referrer_id: str):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT referral_id, display_name FROM referrals WHERE referrer_id=?", (referrer_id,))
    rows = c.fetchall()
    conn.close()
    return rows

# -----------------------------
#  Outline API (создание / удаление ключа)
# -----------------------------
def create_outline_key(name: str):
    """Возвращает (accessUrl, key_id) или (None, None)."""
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
            print(f"Error create key: {resp.status_code}, {resp.text}")
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

# -----------------------------
#  Поток удаления просроченных подписок
# -----------------------------
def subscription_checker():
    while True:
        try:
            conn = get_conn()
            c = conn.cursor()
            c.execute("SELECT user_id, key_id, expiration FROM subscriptions")
            rows = c.fetchall()
            now = datetime.now()
            for user_id, key_id, expiration_str in rows:
                if not expiration_str:
                    continue
                try:
                    exp_dt = datetime.fromisoformat(expiration_str)
                except:
                    continue
                if exp_dt < now:
                    ok = delete_outline_key(key_id)
                    if ok:
                        c.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
                        conn.commit()
                        print(f"Subscription for user {user_id} expired, key {key_id} deleted.")
            conn.close()
        except Exception as e:
            print(f"subscription_checker error: {e}")
        time.sleep(60)

threading.Thread(target=subscription_checker, daemon=True).start()

# -----------------------------
#  YooMoney — генерация ссылки
# -----------------------------
def generate_payment_url(user_id: str, amount: float, description: str) -> str:
    if not Quickpay:
        print("yoomoney не установлен, ссылка не будет сгенерирована")
        return ""
    payment_label = f"vpn_{user_id}_{uuid.uuid4().hex}"
    quickpay = Quickpay(
        receiver=YOOMONEY_RECEIVER,
        quickpay_form="shop",
        targets=description,
        paymentType="AC",
        sum=amount,
        label=payment_label
    )
    return quickpay.base_url

# -----------------------------
#  ГЛАВНАЯ СТРАНИЦА
#  КИБЕРПАНКОВЫЙ СТИЛЬ
# -----------------------------
INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8"/>
  <title>VPN SURFGUARD - Cyberpunk</title>
  <!-- Bootstrap CSS -->
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css">
  <!-- Animate.css -->
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/animate.css/4.1.1/animate.min.css"/>
  <style>
    /* Базовые стили «киберпанк» */
    body {
      background: url('{{ bg_image }}') no-repeat center center fixed;
      background-size: cover;
      color: #e0e0e0;
      font-family: "Lucida Console", Monaco, monospace;
      font-size: 1.25rem; /* увеличить масштаб */
      min-height: 100vh;
      margin: 0; padding: 0;
    }
    .overlay {
      background-color: rgba(0,0,0,0.7);
      min-height: 100vh;
      padding: 60px 20px;
    }
    .main-content {
      max-width: 800px;
      margin: 0 auto;
      text-align: center;
      border: 2px solid rgba(255,255,255,0.2);
      border-radius: 12px;
      padding: 30px;
      box-shadow: 0 0 20px rgba(0,255,255,0.2);
      backdrop-filter: blur(8px);
    }
    h1.heading {
      margin-bottom: 30px;
      text-shadow: 0 0 5px #0ff, 0 0 10px #0ff;
      font-size: 2.2rem;
    }
    .desc {
      margin-bottom: 40px;
      line-height: 1.6;
      white-space: pre-wrap;
      text-shadow: 0 0 4px #0ff;
    }
    /* Кнопки "серо-белые", крупные, с киберпанк подсветкой */
    .btn-cyber {
      background: linear-gradient(135deg, #444, #888);
      color: #fff;
      font-weight: bold;
      font-size: 1.2rem;
      border: 1px solid #bbb;
      border-radius: 8px;
      text-shadow: 0 0 4px #fff;
      box-shadow: 0 0 10px rgba(255,255,255,0.1);
      transition: transform 0.2s, box-shadow 0.2s;
    }
    .btn-cyber:hover {
      transform: scale(1.05);
      box-shadow: 0 0 20px rgba(255,255,255,0.3);
    }
    .btn-animated {
      margin: 10px;
      animation-duration: 1s;
      animation-delay: 0.3s;
    }
    .link-underline {
      color: #fff;
      text-decoration: underline;
    }
  </style>
</head>
<body>
  <div class="overlay">
    <div class="main-content animate__animated animate__fadeInUp">
      <h1 class="heading animate__animated animate__fadeInDown">
        🔥 Добро пожаловать в VPN SURFGUARD!
      </h1>
      <div class="desc">
🚀 Высокая скорость, отсутствие рекламы
🔥 Ускорь видео 4k на YouTube без тормозов
🔐 Надёжный VPN для защиты и анонимности

Нажмите «Получить VPN», чтобы выбрать способ доступа.

📌 Условия использования:
<a href="https://surl.li/owbytz" target="_blank" class="link-underline">https://surl.li/owbytz</a>
      </div>
      <div class="d-grid gap-3 col-10 mx-auto">
        <a href="{{ url_for('get_vpn_main') }}"
           class="btn btn-cyber btn-lg btn-animated animate__animated animate__lightSpeedInLeft">
          Получить VPN
        </a>
        <a href="{{ url_for('page_my_keys') }}"
           class="btn btn-cyber btn-lg btn-animated animate__animated animate__lightSpeedInLeft">
          Мои ключи
        </a>
        <a href="{{ url_for('page_support') }}"
           class="btn btn-cyber btn-lg btn-animated animate__animated animate__lightSpeedInLeft">
          Поддержка
        </a>
        <a href="{{ url_for('page_instruction') }}"
           class="btn btn-cyber btn-lg btn-animated animate__animated animate__lightSpeedInLeft">
          Инструкция
        </a>
        <a href="{{ url_for('page_partner') }}"
           class="btn btn-cyber btn-lg btn-animated animate__animated animate__lightSpeedInLeft">
          Партнёрская программа
        </a>
      </div>
    </div>
  </div>
  <!-- Bootstrap JS -->
  <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/js/bootstrap.bundle.min.js"></script>
</body>
</html>
"""

@app.route("/")
def index():
    return render_template_string(INDEX_HTML, bg_image=BG_IMAGE_URL)

# -----------------------------
#  ПРОЧИЕ СТРАНИЦЫ / ЛОГИКА
# -----------------------------

@app.route("/support")
def page_support():
    return "<h2>Поддержка: @SURFGUARD_VPN_help</h2>"

@app.route("/instruction")
def page_instruction():
    return "<h2>Инструкция по настройке VPN (здесь ваш контент)</h2>"

@app.route("/partner")
def page_partner():
    return """
    <h2>Партнёрская программа</h2>
    <p>Пригласите 5 друзей по вашей ссылке и получите +1 месяц бесплатного VPN.</p>
    <p>(Доработайте логику рефералов под ваши нужды)</p>
    """

@app.route("/get_vpn_main")
def get_vpn_main():
    html = """
    <div style="margin: 40px;">
      <h2>Получить VPN</h2>
      <p>Выберите один из вариантов:</p>
      <ul style="font-size:1.3rem;">
        <li><a href="/free_trial?user_id=DEMO_USER">🎁 Бесплатная неделя</a></li>
        <li><a href="/pay?user_id=DEMO_USER&plan=1m">1 месяц (199₽)</a></li>
        <li><a href="/pay?user_id=DEMO_USER&plan=3m">3 месяца (599₽)</a></li>
        <li><a href="/pay?user_id=DEMO_USER&plan=6m">6 месяцев (1199₽)</a></li>
      </ul>
      <p>DEMO: user_id=DEMO_USER (в реальном решении укажите реальный ID).</p>
    </div>
    """
    return html

@app.route("/free_trial")
def free_trial():
    user_id = request.args.get("user_id", "DEMO_USER")
    if is_free_trial_used(user_id):
        return "<h3>Вы уже использовали бесплатную неделю.</h3>"
    key_name = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} - {user_id}"
    access_url, key_id = create_outline_key(key_name)
    if not access_url:
        return "<h3>Ошибка при создании Outline ключа</h3>"
    expiration = datetime.now() + timedelta(days=FREE_TRIAL_DAYS)
    set_free_trial_used(user_id)
    save_subscription(user_id, access_url, key_id, expiration)
    return f"""
    <h2>Бесплатная неделя активирована!</h2>
    <p>Ваш ключ Outline: <code>{access_url}</code></p>
    <p>Действует до {expiration.strftime('%Y-%m-%d %H:%M')}</p>
    <a href="/">Назад на главную</a>
    """

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
        return "<h3>Неверный план</h3>"
    pay_url = generate_payment_url(user_id, amount, desc)
    if not pay_url:
        return "<h3>Ошибка генерации ссылки на оплату</h3>"
    return f"""
    <h3>{desc} ({amount}₽)</h3>
    <p><a href="{pay_url}" target="_blank">Оплатить</a></p>
    <p>После оплаты <a href="/after_payment?user_id={user_id}&days={days}">нажмите сюда</a>, чтобы активировать ключ.</p>
    <a href="/">На главную</a>
    """

@app.route("/after_payment")
def after_payment():
    """Упрощённый маршрут подтверждения платежа"""
    user_id = request.args.get("user_id", "DEMO_USER")
    days_str = request.args.get("days", "30")
    try:
        days = int(days_str)
    except:
        days = 30
    key_name = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} - {user_id}"
    access_url, key_id = create_outline_key(key_name)
    if not access_url:
        return "<h3>Ошибка создания Outline ключа!</h3>"
    expiration = datetime.now() + timedelta(days=days)
    save_subscription(user_id, access_url, key_id, expiration)
    return f"""
    <h2>Платёж подтверждён (условно)!</h2>
    <p>Подписка действует до {expiration.strftime('%Y-%m-%d %H:%M')}.</p>
    <p>Ваш Outline key: <code>{access_url}</code></p>
    <a href="/">На главную</a>
    """

@app.route("/my_keys")
def page_my_keys():
    user_id = request.args.get("user_id", "DEMO_USER")
    row = get_subscription(user_id)
    if not row:
        return "<h3>У вас нет активной подписки.</h3>"
    outline_key, key_id, expiration_str = row
    if not expiration_str:
        return "<h3>Нет данных об истечении срока</h3>"
    try:
        exp_dt = datetime.fromisoformat(expiration_str)
    except:
        return "<h3>Ошибка парсинга даты</h3>"
    now = datetime.now()
    if exp_dt < now:
        return "<h3>Ваш ключ уже истёк.</h3>"
    remaining = exp_dt - now
    days = remaining.days
    hours, rem = divmod(remaining.seconds, 3600)
    minutes, _ = divmod(rem, 60)
    return f"""
    <h2>Мои ключи</h2>
    <p>Ваш Outline key: <code>{outline_key}</code></p>
    <p>Истекает {exp_dt.strftime('%Y-%m-%d %H:%M')}<br>
       (через {days} дн, {hours} ч, {minutes} мин)</p>
    <a href="/">На главную</a>
    """

# -----------------------------
#  Запуск приложения
# -----------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
