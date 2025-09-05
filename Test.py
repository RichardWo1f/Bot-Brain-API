import webbrowser
import hashlib
import base64
import os
import requests
from requests.packages.urllib3.exceptions import InsecureRequestWarning

requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

from urllib.parse import urlencode, parse_qs
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import ssl
import json
from datetime import datetime

# -- НАСТРОЙКИ --
CLIENT_ID = "c7z6p"
CLIENT_SECRET = "eDWm62flZAqlvk4GGkf7xMXlfyCIPPil"
REDIRECT_URI = "https://localhost:5001"
# Широкий набор прав для максимальной совместимости с разными эндпоинтами
SCOPES = "openid offline_access shared organizationstructure incentives"

# -- ИМЕНА ФАЙЛОВ --
DODO_TOKEN_FILE = 'dodo_token_tester.json'  # Уникальный токен для этого скрипта

# -- Глобальные переменные --
auth_event = threading.Event()
authorization_code = None
dodo_tokens = {}


# --- БЛОК 1: АВТОРИЗАЦИЯ DODO IS API ---

def save_dodo_tokens(tokens):
    global dodo_tokens
    dodo_tokens = tokens
    with open(DODO_TOKEN_FILE, 'w') as f:
        json.dump(tokens, f)


def load_dodo_tokens():
    global dodo_tokens
    if os.path.exists(DODO_TOKEN_FILE):
        with open(DODO_TOKEN_FILE, 'r') as f:
            dodo_tokens = json.load(f)
        return True
    return False


def refresh_dodo_token():
    if 'refresh_token' not in dodo_tokens: return False
    print("[Тестер][Токен] Попытка обновить токен...")
    data = {'grant_type': 'refresh_token', 'refresh_token': dodo_tokens['refresh_token'], 'client_id': CLIENT_ID,
            'client_secret': CLIENT_SECRET}
    try:
        response = requests.post("https://auth.dodois.io/connect/token", data=data,
                                 headers={'Content-Type': 'application/x-www-form-urlencoded'}, verify=False)
        response.raise_for_status()
        save_dodo_tokens(response.json())
        print("[Тестер][Токен] Токен успешно обновлен.")
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


# --- БЛОК 2: ОСНОВНАЯ ЛОГИКА ТЕСТЕРА ---
def main():
    print("[Старт] Запрос дневных продаж по пиццерии.")

    access_token = get_access_token()
    if not access_token:
        print("\n[Выход] Не удалось авторизоваться. Скрипт завершает работу.")
        return

    # -- ПАРАМЕТРЫ ЗАПРОСА --
    # URL и ID пиццерии заданы прямо в коде
    url = "https://api.dodois.io/dodopizza/ru/finances/sales/units/daily"
    params = {
        'units': '5e928e9d6e51929f11ee2f76c6618ca5'
    }

    # Запрашиваем только период у пользователя
    while True:
        try:
            from_date_str = input("\nВведите начальную дату (ГГГГ-ММ-ДД): ").strip()
            to_date_str = input("Введите конечную дату (ГГГГ-ММ-ДД): ").strip()

            # Проверяем формат, чтобы избежать ошибок
            datetime.strptime(from_date_str, "%Y-%m-%d")
            datetime.strptime(to_date_str, "%Y-%m-%d")

            params['fromDate'] = from_date_str
            params['toDate'] = to_date_str
            break
        except ValueError:
            print("[Ошибка] Неверный формат даты. Пожалуйста, используйте ГГГГ-ММ-ДД.")

    # Выполнение запроса
    print("\n[API] Выполняем запрос...")
    try:
        api_headers = {'Authorization': f'Bearer {access_token}'}
        response = requests.get(url, headers=api_headers, params=params, verify=False)
        response.raise_for_status()

        print("[Успех] Ответ от сервера получен:")
        print(json.dumps(response.json(), indent=4, ensure_ascii=False))

    except requests.exceptions.HTTPError as e:
        print(f"\n[Ошибка] HTTP {e.response.status_code}:")
        try:
            print(json.dumps(e.response.json(), indent=4, ensure_ascii=False))
        except json.JSONDecodeError:
            print(e.response.text)
    except requests.exceptions.RequestException as e:
        print(f"\n[Ошибка] Проблема с сетевым подключением: {e}")

    print("\n[Выход] Работа скрипта завершена.")


if __name__ == "__main__":
    main()