import os
import sqlite3
import uuid
import logging
from datetime import datetime, timedelta
from threading import Thread
import time

import requests  # будем использовать для Outline API
from flask import Flask, request, render_template_string, redirect, url_for

# -----------------------------
#  Внешняя библиотека YooMoney
# -----------------------------
try:
    from yoomoney import Quickpay, Client
except ImportError:
    Quickpay = None
    Client = None
    logging.warning("yoomoney не установлен. Установите, иначе оплаты работать не будут.")

# -----------------------------
#  НАСТРОЙКИ / КОНСТАНТЫ
# -----------------------------
# Подставьте свои реальные значения или храните в переменных окружения
YOOMONEY_TOKEN    = os.getenv('YOOMONEY_TOKEN', '4100116412273743.9FF0D8315EF8D02914C839B78EAFF293DC40AF6FF2F0E0BB0B312E709C950E13462F1D21594AF6602C672CE7099E66EF89971092FE5721FD778ED82C94531CE214AF890905832DC355814DA3564B7F27C0F61AC402A9FBE0784E6DF116851ECDA2A8C1DA6BBE1B2B85E72BF04FBFBC61085747E5F662CF0406DB9CB4B36EF809')
YOOMONEY_RECEIVER = os.getenv('YOOMONEY_RECEIVER', '4100116412273743')

# Outline API: 
OUTLINE_API_URL   = os.getenv('OUTLINE_API_URL', 'https://194.87.83.100:12245/ys7r0QWOtNdWJGUDtAvqGw')
OUTLINE_API_KEY   = os.getenv('OUTLINE_API_KEY', '4d18c537-566b-46c3-b937-bcc28378b306')  # Bearer-токен, если нужно
OUTLINE_DISABLE_SSL_CHECK = True  # Иногда надо отключать проверку SSL (небезопасно!)

# Срок бесплатного периода
FREE_TRIAL_DAYS = 7

# -----------------------------
#  ИНИЦИАЛИЗАЦИЯ ПРИЛОЖЕНИЯ
# -----------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Создаём (если нет) базу данных SQLite
DB_NAME = "vpn_app.db"

def init_db():
    with sqlite3.connect(DB_NAME) as conn:
        c = conn.cursor()
        # Таблица пользователей
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                free_trial_used INTEGER DEFAULT 0
            )
        """)
        # Таблица подписок
        c.execute("""
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id TEXT PRIMARY KEY,
                outline_key TEXT,
                key_id TEXT,
                expiration TEXT  -- datetime в формате ISO
            )
        """)
        conn.commit()

init_db()

# -----------------------------
#  УТИЛИТЫ ДЛЯ РАБОТЫ С БАЗОЙ
# -----------------------------
def get_db_connection():
    return sqlite3.connect(DB_NAME, check_same_thread=False)

def is_free_trial_used(user_id: str) -> bool:
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT free_trial_used FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return False
    return bool(row[0])

def set_free_trial_used(user_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO users (user_id, free_trial_used)
        VALUES (?, 1)
        ON CONFLICT(user_id) DO UPDATE SET free_trial_used=1
    """, (user_id,))
    conn.commit()
    conn.close()

def save_subscription(user_id: str, outline_key: str, key_id: str, expiration: datetime):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("""
        INSERT INTO subscriptions (user_id, outline_key, key_id, expiration)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET outline_key=?, key_id=?, expiration=?
    """, (user_id, outline_key, key_id, expiration.isoformat(), outline_key, key_id, expiration.isoformat()))
    conn.commit()
    conn.close()

def get_subscription(user_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("SELECT outline_key, key_id, expiration FROM subscriptions WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row  # (outline_key, key_id, expiration_str) или None

def remove_subscription(user_id: str):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

# -----------------------------
#  OUTLINE API: СОЗДАНИЕ / УДАЛЕНИЕ КЛЮЧА
# -----------------------------
def create_outline_key(name: str):
    """
    Создаёт ключ в Outline. Возвращает (access_url, key_id) или (None, None) при ошибке.
    """
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
            logging.error(f"Create key failed: {resp.status_code}, {resp.text}")
    except Exception as e:
        logging.error(f"Error create_outline_key: {e}")
    return None, None

def delete_outline_key(key_id: str):
    """
    Удаляет ключ в Outline.
    """
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
        logging.error(f"Error delete_outline_key: {e}")
        return False

# -----------------------------
#  ФОНОВАЯ УТИЛИТА: УДАЛЕНИЕ ПРОСРОЧЕННЫХ ПОДПИСОК
# -----------------------------
def subscription_checker():
    """
    Фоновый поток: каждые N минут проверяет, не истекли ли у кого-то подписки,
    и при необходимости удаляет ключ Outline + запись из БД.
    """
    while True:
        try:
            conn = get_db_connection()
            c = conn.cursor()
            c.execute("SELECT user_id, key_id, expiration FROM subscriptions")
            rows = c.fetchall()
            now = datetime.now()
            for user_id, key_id, expiration_str in rows:
                if not expiration_str:
                    continue
                try:
                    expiration_dt = datetime.fromisoformat(expiration_str)
                except:
                    continue
                if expiration_dt < now:
                    # Срок подписки истёк — удаляем
                    ok = delete_outline_key(key_id)
                    if ok:
                        c.execute("DELETE FROM subscriptions WHERE user_id=?", (user_id,))
                        conn.commit()
                        logging.info(f"Subscription {user_id} expired, key deleted.")
            conn.close()
        except Exception as e:
            logging.error(f"subscription_checker error: {e}")
        time.sleep(60)  # чекать раз в минуту (для примера)

Thread(target=subscription_checker, daemon=True).start()

# -----------------------------
#  СКЕЛЕТ ЛОГИКИ ОПЛАТЫ (YooMoney)
# -----------------------------
def generate_payment_url(user_id: str, amount: float, description: str) -> str:
    """
    Генерируем ссылку на оплату через YooMoney.
    В реальном использовании стоит также настраивать callbackURL, проверять статус платежа и т.д.
    """
    if not Quickpay:
        # Если yoomoney не установлен
        return ""

    payment_label = f"vpn_{user_id}_{uuid.uuid4().hex}"
    quickpay = Quickpay(
        receiver=YOOMONEY_RECEIVER,
        quickpay_form="shop",
        targets=description,
        paymentType="AC",   # банковская карта
        sum=amount,
        label=payment_label
    )
    invoice_url = quickpay.base_url
    return invoice_url

# -----------------------------
#  ШАБЛОН ГЛАВНОЙ СТРАНИЦЫ
# -----------------------------
INDEX_HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8"/>
  <title>VPN SURFGUARD</title>
  <!-- Bootstrap -->
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body {
      background: #f0f0f0;
    }
    .header {
      margin-top: 40px;
      text-align: center;
    }
    .card {
      margin: 20px;
      border-radius: 10px;
    }
    .container {
      max-width: 900px;
      margin: 0 auto;
    }
    .vpn-logo {
      width: 80px;
    }
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <img src="https://via.placeholder.com/80?text=VPN" class="vpn-logo" alt="VPN Logo" />
      <h1 class="mt-3">VPN SURFGUARD</h1>
      <p class="text-muted">Современный VPN-сервис с мгновенным доступом</p>
    </div>

    <div class="card p-4">
      <h4>Добро пожаловать!</h4>
      <p>Введите Ваш уникальный идентификатор (например, номер телефона, email или любой ID), чтобы мы могли привязать подписку.</p>
      <form method="POST" action="/set_user_id">
        <div class="mb-3">
          <label for="user_id" class="form-label">Ваш ID</label>
          <input type="text" class="form-control" id="user_id" name="user_id" placeholder="Например, email или nickname" required>
        </div>
        <button type="submit" class="btn btn-primary">Далее</button>
      </form>
    </div>

    <div class="text-center text-muted mt-5 mb-3">
      <small>© VPN SURFGUARD, 2025</small>
    </div>
  </div>
</body>
</html>
"""

# -----------------------------
#  СТРАНИЦА ВЫБОРА ПОДПИСКИ
# -----------------------------
SUBSCRIPTIONS_HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8"/>
  <title>VPN SURFGUARD - Подписка</title>
  <!-- Bootstrap -->
  <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
  <style>
    body {
      background: #f0f0f0;
    }
    .container {
      max-width: 900px;
      margin: 0 auto;
      margin-top: 40px;
    }
    .card {
      margin: 20px;
    }
  </style>
</head>
<body>
  <div class="container">
    <h2>Подписка для {{ user_id }}</h2>
    <p>Статус бесплатной недели: 
      {% if free_used %}
        <span class="badge bg-danger">Уже использовано</span>
      {% else %}
        <span class="badge bg-success">Доступно</span>
      {% endif %}
    </p>

    {% if current_key %}
      <div class="card p-3">
        <h5>Ваш текущий доступ:</h5>
        <p>Конфиг Outline (скопируйте и вставьте в приложение Outline):</p>
        <pre style="background: #eee; padding: 10px;">{{ current_key }}</pre>
        <p>Подписка действует до: <b>{{ expiration }}</b></p>
      </div>
    {% else %}
      <p>У вас нет активной подписки</p>
    {% endif %}

    <hr/>
    <h4>Получить VPN</h4>
    <div class="row">
      <div class="col-md-4 mb-3">
        <div class="card p-3">
          <h5>1 неделя бесплатно</h5>
          <p>Пробный период</p>
          {% if free_used %}
            <button class="btn btn-secondary" disabled>Уже использовано</button>
          {% else %}
            <a href="/free_trial?user_id={{ user_id }}" class="btn btn-success">Активировать</a>
          {% endif %}
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card p-3">
          <h5>1 месяц</h5>
          <p>199₽</p>
          <a href="/pay?user_id={{ user_id }}&period=1m" class="btn btn-primary">Оплатить</a>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card p-3">
          <h5>3 месяца</h5>
          <p>599₽</p>
          <a href="/pay?user_id={{ user_id }}&period=3m" class="btn btn-primary">Оплатить</a>
        </div>
      </div>
      <div class="col-md-4 mb-3">
        <div class="card p-3">
          <h5>6 месяцев</h5>
          <p>1199₽</p>
          <a href="/pay?user_id={{ user_id }}&period=6m" class="btn btn-primary">Оплатить</a>
        </div>
      </div>
    </div>

    <a href="/" class="btn btn-link mt-3">Выбрать другого пользователя</a>
  </div>
</body>
</html>
"""

# -----------------------------
#  МАРШРУТЫ ПРИЛОЖЕНИЯ
# -----------------------------

@app.route("/")
def index():
    """
    Главная страница — предлагаем ввести user_id
    """
    return render_template_string(INDEX_HTML)

@app.route("/set_user_id", methods=["POST"])
def set_user_id():
    """
    Обработка введённого user_id. Редирект на страницу выбора подписки.
    """
    user_id = request.form.get("user_id")
    if not user_id:
        return "Ошибка: не указан user_id", 400
    return redirect(url_for("subscriptions_page", user_id=user_id))

@app.route("/subscriptions")
def subscriptions_page():
    """
    Страница с кнопками: free_trial, покупка подписки и т.п.
    """
    user_id = request.args.get("user_id", "")
    if not user_id:
        return redirect("/")

    used = is_free_trial_used(user_id)
    subs = get_subscription(user_id)
    current_key, _, expiration_str = subs if subs else (None, None, None)

    expiration = ""
    if expiration_str:
        try:
            dt = datetime.fromisoformat(expiration_str)
            expiration = dt.strftime("%Y-%m-%d %H:%M")
        except:
            expiration = expiration_str

    return render_template_string(
        SUBSCRIPTIONS_HTML,
        user_id=user_id,
        free_used=used,
        current_key=current_key,
        expiration=expiration
    )

@app.route("/free_trial")
def free_trial():
    """
    Активирует бесплатную неделю
    """
    user_id = request.args.get("user_id", "")
    if not user_id:
        return redirect("/")

    # Проверяем, не использовал ли уже
    if is_free_trial_used(user_id):
        return "Вы уже использовали бесплатную неделю. <a href='/subscriptions?user_id={0}'>Назад</a>".format(user_id)

    # Пытаемся создать ключ
    # Имя ключа — дата + user_id
    key_name = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} - {user_id}"
    access_url, key_id = create_outline_key(key_name)
    if not access_url:
        return "Ошибка создания ключа Outline. Попробуйте позже."

    # Запоминаем в БД
    set_free_trial_used(user_id)
    expiration = datetime.now() + timedelta(days=FREE_TRIAL_DAYS)
    save_subscription(user_id, access_url, key_id, expiration)

    return redirect(url_for("subscriptions_page", user_id=user_id))

@app.route("/pay")
def pay():
    """
    Генерируем ссылку на оплату для подписки
    """
    user_id = request.args.get("user_id", "")
    period_code = request.args.get("period", "")

    if not user_id:
        return redirect("/")

    # Определяем цену и срок
    if period_code == "1m":
        amount = 199
        days = 30
        desc  = "Оплата VPN (1 месяц)"
    elif period_code == "3m":
        amount = 599
        days = 90
        desc  = "Оплата VPN (3 месяца)"
    elif period_code == "6m":
        amount = 1199
        days = 180
        desc  = "Оплата VPN (6 месяцев)"
    else:
        return "Неверный период"

    pay_url = generate_payment_url(user_id, amount, desc)
    if not pay_url:
        return "Ошибка генерации ссылки на оплату (yoomoney не настроен?)."

    # Сохраняем выбранное количество дней в сессии или во «временном» месте,
    # но в упрощённом примере просто добавим его к URL, чтобы
    # после оплаты пользователь вручную возвращался на /after_payment
    # В реальности нужно настраивать callbackURL и проверять реальный статус платежа.
    return f"""
    <h3>Оплата {desc} ({amount}₽)</h3>
    <p>Ссылка на оплату: <a href="{pay_url}" target="_blank">Перейти к оплате</a></p>
    <p>После оплаты вернитесь и нажмите:
       <a href="/after_payment?user_id={user_id}&days={days}">Подтвердить платёж</a></p>
    <p><a href='/subscriptions?user_id={user_id}'>Назад</a></p>
    """

@app.route("/after_payment")
def after_payment():
    """
    Упрощённый маршрут «Подтвердить платёж».
    В реальном решении здесь должна быть проверка: действительно ли пользователь оплатил.
    """
    user_id = request.args.get("user_id", "")
    days = request.args.get("days", "30")
    if not user_id or not days.isdigit():
        return "Ошибка"

    # Генерируем Outline Key
    key_name = f"{datetime.now().strftime('%Y-%m-%d %H:%M')} - {user_id}"
    access_url, key_id = create_outline_key(key_name)
    if not access_url:
        return "Ошибка создания Outline ключа!"

    expiration = datetime.now() + timedelta(days=int(days))
    save_subscription(user_id, access_url, key_id, expiration)

    return f"""
    <h3>Платёж подтверждён (условно)!</h3>
    <p>Текущая подписка действует до {expiration.strftime('%Y-%m-%d %H:%M')}.</p>
    <p>Ключ Outline (скопируйте в приложение):<br/>
       <code>{access_url}</code></p>
    <a href="/subscriptions?user_id={user_id}">Вернуться</a>
    """


# ЗАПУСК ПРИЛОЖЕНИЯ
# -----------------------------
if __name__ == "__main__":
    # Считываем значение PORT из переменных окружения
    port_str = os.getenv("PORT", "8080")

    try:
        PORT = int(port_str)
    except ValueError:
        # Если значение не получилось конвертировать в int,
        # используем порт 8080
        PORT = 8080

    # Запускаем Flask на 0.0.0.0:PORT
    app.run(host="0.0.0.0", port=PORT)
