import webbrowser
import hashlib
import base64
import os
import time
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

from urllib.parse import urlencode, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import ssl
import json
from datetime import datetime
import pytz

# -- НАСТРОЙКИ --
CLIENT_ID = "c7z6p"
CLIENT_SECRET = "eDWm62flZAqlvk4GGkf7xMXlfyCIPPil"
REDIRECT_URI = "https://localhost:5001"
SCOPES = "openid offline_access shared organizationstructure"
TARGET_ORGANIZATION_ID = "000d3a240c719a8711e69b3935eba0ac"

# -- Файлы --
DODO_TOKEN_FILE = 'dodo_token_fetcher.json'
PIZZERIAS_FILE = 'ID пиццерий.txt'
REVIEWS_DATA_FILE = 'reviews_data.json'

# -- Глобальные переменные --
auth_event = threading.Event()
authorization_code = None
dodo_tokens = {}


# --- БЛОК АВТОРИЗАЦИИ DODO IS API ---

def save_dodo_tokens(tokens):
    global dodo_tokens
    dodo_tokens = tokens
    with open(DODO_TOKEN_FILE, 'w') as f: json.dump(tokens, f)


def load_dodo_tokens():
    global dodo_tokens
    if os.path.exists(DODO_TOKEN_FILE):
        with open(DODO_TOKEN_FILE, 'r') as f: dodo_tokens = json.load(f)
        return True
    return False


def refresh_dodo_token():
    if 'refresh_token' not in dodo_tokens: return False
    print("[Сборщик][Токен] Попытка обновить токен...")
    data = {'grant_type': 'refresh_token', 'refresh_token': dodo_tokens['refresh_token'], 'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET}
    try:
        response = requests.post("https://auth.dodois.io/connect/token", data=data,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'}, verify=False)
        response.raise_for_status()
        save_dodo_tokens(response.json())
        print("[Сборщик][Токен] Токен успешно обновлен.")
        return True
    except requests.RequestException:
        if os.path.exists(DODO_TOKEN_FILE): os.remove(DODO_TOKEN_FILE)
        return False


def get_access_token():
    if not load_dodo_tokens() or not refresh_dodo_token():
        if not perform_full_auth_flow(): return None
    return dodo_tokens.get('access_token')


def perform_full_auth_flow():
    global authorization_code
    authorization_code = None
    auth_event.clear()

    try:
        server = HTTPServer(('localhost', 5001), CallbackHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile='cert.pem', keyfile='key.pem')
        server.socket = context.wrap_socket(server.socket, server_side=True)
    except FileNotFoundError:
        print("\n[Критическая ошибка] Не найдены файлы сертификата 'cert.pem' и 'key.pem'.")
        return False

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    code_verifier, code_challenge = generate_pkce_codes()
    auth_params = {'client_id': CLIENT_ID, 'scope': SCOPES, 'response_type': 'code', 'redirect_uri': REDIRECT_URI,
                   'code_challenge': code_challenge, 'code_challenge_method': 'S256'}
    auth_url = f"https://auth.dodois.io/connect/authorize?{urlencode(auth_params)}"
    print(f"\n[АВТОРИЗАЦИЯ] Требуется ручной вход. Пожалуйста, откройте эту ссылку в браузере:\n{auth_url}\n")

    auth_event.wait(timeout=300)
    server.shutdown()

    if not authorization_code:
        print("[Авторизация] Ошибка: не удалось получить код авторизации.")
        return False

    try:
        token_data = {'client_id': CLIENT_ID, 'client_secret': CLIENT_SECRET, 'grant_type': 'authorization_code',
                      'code': authorization_code, 'code_verifier': code_verifier, 'redirect_uri': REDIRECT_URI,
                      'scope': SCOPES}
        response = requests.post("https://auth.dodois.io/connect/token", data=token_data,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'}, verify=False)
        response.raise_for_status()
        save_dodo_tokens(response.json())
        print("[Авторизация] Успешно! Токены сохранены.")
        return True
    except requests.exceptions.HTTPError as e:
        print(f"\n[Критическая ошибка авторизации] {e.response.text}")
        return False


def generate_pkce_codes():
    code_verifier = base64.urlsafe_b64encode(os.urandom(40)).decode('utf-8').rstrip('=')
    challenge_bytes = hashlib.sha256(code_verifier.encode('utf-8')).digest()
    code_challenge = base64.urlsafe_b64encode(challenge_bytes).decode('utf-8').rstrip('=')
    return code_verifier, code_challenge


class CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global authorization_code
        if '?' in self.path and 'code' in self.path:
            parsed_path = parse_qs(self.path.split('?', 1)[1])
            authorization_code = parsed_path['code'][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write("<h1>Авторизация прошла успешно!</h1><p>Можете закрыть это окно.</p>".encode('utf-8'))
            auth_event.set()


# --- БЛОК СБОРА ДАННЫХ ---

def update_pizzerias_list(access_token):
    print("\n[Пиццерии] Запрашиваем полный список заведений...")
    all_units, skip, take, is_end_reached = [], 0, 100, False
    while not is_end_reached:
        params = {'countryId': 'ru', 'businessId': '63d4829611ea45c8ae71394860a2481c', 'skip': skip, 'take': take}
        try:
            response = requests.get("https://api.dodois.io/dodopizza/ru/units",
                                    headers={'Authorization': f'Bearer {access_token}'}, params=params, verify=False)
            response.raise_for_status()
            data = response.json()
            new_units = data.get('units', [])
            if not new_units: break
            all_units.extend(new_units)
            is_end_reached = data.get('isEndOfListReached', True)
            skip += take
        except requests.RequestException as e:
            print(f"[Ошибка] Не удалось получить список заведений: {e}")
            return None

    filtered_pizzerias = [p for p in all_units if
                          p.get('organizationId') == TARGET_ORGANIZATION_ID and p.get('type') == 'Store']
    with open(PIZZERIAS_FILE, 'w', encoding='utf-8') as f:
        for p in filtered_pizzerias: f.write(f"{p['id']} - {p['name']}\n")
    print(f"[Пиццерии] Список из {len(filtered_pizzerias)} пиццерий сохранен.")
    return [p['id'] for p in filtered_pizzerias]


def fetch_reviews(access_token, pizzeria_ids):
    print("[Отзывы] Запрашиваем свежие отзывы...")
    all_reviews = []
    moscow_tz = pytz.timezone('Europe/Moscow')

    for i in range(0, len(pizzeria_ids), 30):
        chunk = pizzeria_ids[i:i + 30]
        params = {'units': ",".join(chunk)}
        # ИСПРАВЛЕНО: URL для получения отзывов был обновлен на правильный
        url = "https://api.dodois.io/dodopizza/customer-feedback/recent-feedbacks"
        try:
            response = requests.get(url, headers={'Authorization': f'Bearer {access_token}'}, params=params,
                                    verify=False)
            response.raise_for_status()
            data = response.json()

            reviews_data = data.get('orderFeedbacks', [])
            for review in reviews_data:
                date_str = review.get('orderCreatedAt')
                if date_str and isinstance(date_str, str):
                    try:
                        if date_str.endswith('Z'):
                            date_str = date_str[:-1] + '+00:00'

                        utc_dt = datetime.fromisoformat(date_str)

                        if utc_dt.tzinfo is None:
                            utc_dt = pytz.utc.localize(utc_dt)

                        moscow_dt = utc_dt.astimezone(moscow_tz)
                        review['orderCreatedAt'] = moscow_dt.isoformat()
                    except ValueError as e:
                        print(f"  [Предупреждение] Не удалось конвертировать время '{date_str}': {e}")
                else:
                    print(
                        f"  [Предупреждение] Пропущен отзыв с некорректной датой (значение: {review.get('orderCreatedAt')}).")

            all_reviews.extend(reviews_data)

        except requests.RequestException as e:
            print(f"[Ошибка] Не удалось получить отзывы: {e}")

    with open(REVIEWS_DATA_FILE, 'w', encoding='utf-8') as f:
        json.dump(all_reviews, f, ensure_ascii=False, indent=4)

    print(f"[Отзывы] Получено и сохранено {len(all_reviews)} отзывов.")


# --- ОСНОВНАЯ ЛОГИКА ---
def main():
    access_token = get_access_token()
    if not access_token:
        print("\n[Выход] Не удалось авторизоваться.")
        return

    pizzeria_ids = []
    last_pizzeria_update_time = 0
    PIZZERIA_UPDATE_INTERVAL = 24 * 60 * 60

    while True:
        current_time = time.time()

        if not pizzeria_ids or (current_time - last_pizzeria_update_time > PIZZERIA_UPDATE_INTERVAL):
            token = get_access_token()
            if not token:
                print("\n[Выход] Потерян токен доступа.")
                break

            pizzeria_ids = update_pizzerias_list(token)
            if not pizzeria_ids:
                print("\n[Выход] Не удалось получить список пиццерий. Повторная попытка через час.")
                time.sleep(3600)
                continue

            last_pizzeria_update_time = current_time

        token = get_access_token()
        if not token:
            print("\n[Выход] Потерян токен доступа.")
            break

        if pizzeria_ids:
            fetch_reviews(token, pizzeria_ids)

        print(f"\nСледующая проверка отзывов через 5 минут...")
        time.sleep(300)


if __name__ == "__main__":
    main()