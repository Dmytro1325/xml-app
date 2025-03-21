from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import os
import threading
import gspread
import xml.etree.ElementTree as ET
import json
import time
import requests
import asyncio
import re
from datetime import datetime
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest
import random
import hashlib
import urllib.parse

# 🔹 Конфігурація
MASTER_SHEET_ID = "1z16Xcj_58R2Z-JGOMuyx4GpVdQqDn1UtQirCxOrE_hc"
XML_DIR = "/output"
LOG_DIR = "/logs"
DEBUG_LOG_FILE = os.path.join(LOG_DIR, "debug_log.html")  # Файл, а не директорія!
UPDATE_INTERVAL = 1800  # 30 хвилин
price_hash_cache = {}

# 🔹 Створення директорій
for dir_path in [XML_DIR, os.path.dirname(DEBUG_LOG_FILE)]:
    os.makedirs(dir_path, exist_ok=True)




# 🔹 Функція логування
def log_to_file(content):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {content}\n"

    with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_entry)

    #print(log_entry.strip())  # Виводимо в консоль

# 🔹 Авторизація Google Sheets
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
TOKEN_JSON = os.getenv("TOKEN_JSON")
if not GOOGLE_CREDENTIALS or not TOKEN_JSON:
    raise ValueError("❌ GOOGLE_CREDENTIALS або TOKEN_JSON відсутні!")

TOKEN_FILE = json.loads(TOKEN_JSON)
CREDENTIALS_FILE = json.loads(GOOGLE_CREDENTIALS)

def get_google_client():
    creds = Credentials.from_authorized_user_info(TOKEN_FILE)
    if not creds or not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(GoogleRequest(requests.Session()))
            log_to_file("🔄 Токен оновлено")
        else:
            flow = InstalledAppFlow.from_client_config(
                CREDENTIALS_FILE, ["https://www.googleapis.com/auth/spreadsheets"]
            )
            creds = flow.run_local_server(port=8080)
    return gspread.authorize(creds)

client = get_google_client()
spreadsheet = client.open_by_key(MASTER_SHEET_ID)

# 🔹 Функція безпечного отримання значень
def safe_get_value(row, column_letter, default_value="-"):
    """
    Отримує значення комірки, враховуючи колонку у форматі 'A', 'B', 'C'...
    - Перевіряє, чи є значення в межах рядка
    - Видаляє зайві пробіли та приховані символи
    - Завжди повертає рядок
    """
    try:
        if column_letter and column_letter.isalpha():
            col_index = ord(column_letter.upper()) - 65  # A -> 0, B -> 1, C -> 2 ...
            if len(row) > col_index:
                value = str(row[col_index]).strip()
                return value if value else default_value
    except Exception as e:
        log_to_file(f"⚠️ Помилка при отриманні значення ({column_letter}): {e}")

    return default_value

## 🔹 Функція для очищення ціни
def clean_price(value):
    """
    Очищає та форматує значення ціни:
    - Видаляє всі нечислові символи, крім коми та крапки
    - Якщо є десятковий роздільник, зберігає лише цілу частину
    - Повертає значення у вигляді рядка
    """
    try:
        if not value:
            return "0"

        value = re.sub(r"[^\d,\.]", "", value)

        # Якщо є десятковий роздільник, залишаємо лише цілу частину
        if "," in value:
            value = value.split(",")[0]
        elif "." in value:
            value = value.split(".")[0]

        return value if value else "0"

    except Exception as e:
        log_to_file(f"⚠️ Помилка обробки ціни: {value} - {e}")
        return "0"


# 🔹 Функція генерації XML
# 🔹 Функція для створення XML
def create_xml(supplier_id, supplier_name, sheet_id, columns):
    xml_file = os.path.join(XML_DIR, f"{supplier_id}.xml")
    log_to_file(f"📥 Обробка: {supplier_name} ({sheet_id})")

    retry_count = 0
    max_retries = 5  # Спроба до 5 разів при помилці 429

    while retry_count < max_retries:
        try:
            spreadsheet = client.open_by_key(sheet_id)
            sheets = spreadsheet.worksheets()
            combined_data = []

            for sheet in sheets:
                data = sheet.get_all_values()
                if len(data) < 2:
                    log_to_file(f"⚠️ Аркуш {sheet.title} порожній")
                    continue
                combined_data.extend(data[1:])  # Пропускаємо заголовки

            if not combined_data:
                log_to_file(f"⚠️ {supplier_name} немає даних")
                return

            root = ET.Element("products")
            processed_count = 0
            skipped_count = 0

            for row in combined_data:
                product_id = safe_get_value(row, columns.get("ID"))
                name = safe_get_value(row, columns.get("Name"))
                stock = safe_get_value(row, columns.get("Stock"), "true")
                price = clean_price(safe_get_value(row, columns.get("Price"), "0"))
                sku = safe_get_value(row, columns.get("SKU"))
                rrp = clean_price(safe_get_value(row, columns.get("RRP")))
                currency = safe_get_value(row, columns.get("Currency"), "UAH")

                # Пропускаємо товари без обов’язкових полів
                if not product_id or not name or not price:
                    log_to_file(f"❌ Пропускаємо рядок: {row}, оскільки відсутні ID, Name або Price")
                    skipped_count += 1
                    continue

                # Лог перед додаванням у XML
                log_to_file(f"   ✅ Додаємо товар: id='{product_id}', name='{name}', price='{price}', stock='{stock}'")

                product = ET.SubElement(root, "product")
                ET.SubElement(product, "id").text = product_id
                ET.SubElement(product, "name").text = name
                ET.SubElement(product, "stock").text = stock
                ET.SubElement(product, "price").text = price
                ET.SubElement(product, "currency").text = currency

                if sku:
                    ET.SubElement(product, "sku").text = sku
                if rrp and rrp != "0":
                    ET.SubElement(product, "rrp").text = rrp

                processed_count += 1

            # Збереження XML
            ET.ElementTree(root).write(xml_file, encoding="utf-8", xml_declaration=True)
            log_to_file(f"✅ XML {xml_file} збережено ({processed_count} товарів, пропущено {skipped_count})")

            time.sleep(random.uniform(1.5, 2.5))  # Запобігаємо перевантаженню API
            return

        except gspread.exceptions.APIError as e:
            if "429" in str(e):
                retry_count += 1
                wait_time = retry_count * 20
                log_to_file(f"⚠️ Ліміт перевищено. Повторна спроба {retry_count}/{max_retries} через {wait_time} сек.")
                time.sleep(wait_time)
            else:
                log_to_file(f"❌ Помилка доступу до {supplier_name}: {e}")
                return

    log_to_file(f"❌ Всі {max_retries} спроби обробити {supplier_name} провалилися.")


def get_price_hash(sheet):
    """
    Генерує хеш для даних з Google Sheets, щоб визначити, чи змінилися вони.
    """
    try:
        data = sheet.get_all_values()
        data_str = json.dumps(data, sort_keys=True)  # Конвертуємо в JSON
        return hashlib.md5(data_str.encode()).hexdigest()  # Повертаємо MD5-хеш
    except Exception as e:
        log_to_file(f"⚠️ Помилка генерації хешу для {sheet.title}: {e}")
        return None

async def periodic_update():
    """
    Фоновий процес, який оновлює тільки ті XML-файли, які змінилися,
    з урахуванням кешу та обмеження на запити.
    Використовує динамічний мапінг полів з головної таблиці.
    """
    while True:
        log_to_file("🔄 [Auto-Update] Починаємо перевірку змін у Google Sheets...")

        try:
            supplier_data = spreadsheet.worksheet("Sheet1").get_all_records()
        except gspread.exceptions.APIError as e:
            log_to_file(f"❌ Помилка доступу до головної таблиці: {e}")
            await asyncio.sleep(UPDATE_INTERVAL)  # Чекаємо 30 хвилин
            continue

        updated_suppliers = []
        skipped_suppliers = []
        batch_size = 5  # Обробляємо по 5 постачальників за один цикл

        for i in range(0, len(supplier_data), batch_size):
            batch = supplier_data[i:i + batch_size]

            for supplier in batch:
                supplier_id = str(supplier["Post_ID"])
                supplier_name = supplier["Supplier Name"]
                sheet_id = supplier["Google Sheet ID"]

                if supplier_id in skipped_suppliers:
                    log_to_file(f"⚠️ {supplier_name}: Пропускаємо, бо в попередньому циклі було перевищено ліміт API.")
                    continue

                # 📌 Динамічно отримуємо мапінг полів для кожного постачальника
                columns = {
                    "ID": supplier["ID Column"] if supplier["ID Column"] != "-" else None,
                    "Name": supplier["Name Column"] if supplier["Name Column"] != "-" else None,
                    "Stock": supplier["Stock Column"] if supplier["Stock Column"] != "-" else None,
                    "Price": supplier["Price Column"] if supplier["Price Column"] != "-" else None,
                    "SKU": supplier["SKU Column"] if supplier["SKU Column"] != "-" else None,
                    "RRP": supplier["RRP Column"] if supplier["RRP Column"] != "-" else None,
                    "Currency": supplier["Currency Column"] if supplier["Currency Column"] != "-" else None
                }

                retry_count = 0
                max_retries = 5  # Повторюємо до 5 разів у разі помилки

                while retry_count < max_retries:
                    try:
                        sheet = client.open_by_key(sheet_id).sheet1
                        await asyncio.sleep(random.uniform(2, 5))  # Запобігаємо перевантаженню API
                        
                        new_hash = get_price_hash(sheet)

                        if supplier_id in price_hash_cache and price_hash_cache[supplier_id] == new_hash:
                            log_to_file(f"⏭️ {supplier_name}: Немає змін, пропускаємо...")
                            break  # Виходимо з циклу while

                        price_hash_cache[supplier_id] = new_hash  # Оновлюємо кеш

                        # ✅ Використовуємо отримані **динамічні поля**
                        create_xml(supplier_id, supplier_name, sheet_id, columns)

                        updated_suppliers.append(supplier_name)
                        break  # Виходимо з циклу while після успішного виконання

                    except gspread.exceptions.APIError as e:
                        if "429" in str(e):
                            retry_count += 1
                            wait_time = retry_count * 20
                            log_to_file(f"⚠️ Ліміт запитів вичерпано для {supplier_name}. Повторна спроба {retry_count}/{max_retries} через {wait_time} сек.")
                            await asyncio.sleep(wait_time)  # Чекаємо перед повторною спробою
                        else:
                            log_to_file(f"❌ Помилка обробки {supplier_name}: {e}")
                            break  # Виходимо з циклу while, якщо це не помилка 429

                if retry_count == max_retries:
                    log_to_file(f"❌ {supplier_name}: Всі {max_retries} спроби провалилися. Пропускаємо.")
                    skipped_suppliers.append(supplier_id)

        log_to_file(f"✅ [Auto-Update] Оновлено {len(updated_suppliers)} постачальників, чекаємо на наступний цикл...")
        await asyncio.sleep(UPDATE_INTERVAL)  # Чекаємо 30 хвилин до наступної перевірки



# 🔹 API
app = FastAPI()
templates = Jinja2Templates(directory="/app/templates")

@app.get("/output/", response_class=HTMLResponse)
def list_output_files(request: Request):
    """
    Генерує HTML-сторінку зі списком файлів у папці /output/
    """
    try:
        files = os.listdir(XML_DIR)
        files = sorted(files)  # Сортуємо за алфавітом
    except FileNotFoundError:
        files = []
    return templates.TemplateResponse("file_list.html", {"request": request, "files": files})

app.mount("/output/", StaticFiles(directory=os.path.abspath(XML_DIR)), name="output")  
 

@app.get("/XML_prices/google_sheet_to_xml/files")
def list_files():
    return {"files": [f for f in os.listdir(XML_DIR) if f.endswith(".xml")]}

@app.get("/XML_prices/google_sheet_to_xml/download/{filename}")
def download_file(filename: str):
    file_path = os.path.join(XML_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=filename)
    raise HTTPException(status_code=404, detail="Файл не знайдено")

@app.delete("/XML_prices/google_sheet_to_xml/delete/{filename}")
def delete_file(filename: str):
    file_path = os.path.join(XML_DIR, filename)
    if os.path.exists(file_path):
        os.remove(file_path)
        return {"status": "success", "message": f"Файл {filename} видалено."}
    raise HTTPException(status_code=404, detail=f"Файл {filename} не знайдено.")

@app.delete("/XML_prices/google_sheet_to_xml/delete_all")
def delete_all_files():
    files = os.listdir(XML_DIR)
    for file in files:
        os.remove(os.path.join(XML_DIR, file))
    return {"status": "success", "message": "Всі файли у папці output видалено."}

@app.get("/logs/debug", response_class=HTMLResponse)
def view_debug_log():
    if os.path.exists(DEBUG_LOG_FILE):
        return FileResponse(DEBUG_LOG_FILE)
    raise HTTPException(status_code=404, detail="Файл логів не знайдено.")



@app.on_event("startup")
async def startup_event():
    asyncio.ensure_future(periodic_update())  # Запускаємо фоновий процес оновлення XML


# 🔹 Створення директорій для логів
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(os.path.join(LOG_DIR, "debug_logs"), exist_ok=True)  # Виправлення для вкладених папок

@app.get("/logs/", response_class=HTMLResponse)
def list_logs(request: Request):
    """
    Виводить список файлів логів у вигляді HTML-таблиці
    """
    try:
        log_files = [
        {"name": f, "size": os.path.getsize(os.path.join(LOG_DIR, f))}
        for f in os.listdir(LOG_DIR) if os.path.isfile(os.path.join(LOG_DIR, f))
        ]
    except FileNotFoundError:
        log_files = []

    return templates.TemplateResponse("log_list.html", {"request": request, "logs": log_files})


app.mount("/logs/", StaticFiles(directory=os.path.abspath(LOG_DIR)), name="logs")


@app.get("/logs/{filename}", response_class=HTMLResponse)
def view_log(filename: str):
    """
    Відображає вміст лог-файлу у браузері з покращеним форматуванням.
    """
    safe_filename = urllib.parse.unquote(filename)  # Розкодування URL (якщо містить пробіли або спецсимволи)
    file_path = os.path.join(LOG_DIR, safe_filename)

    # Безпека: перевіряємо, чи файл знаходиться в межах LOG_DIR
    if not file_path.startswith(os.path.abspath(LOG_DIR)):
        raise HTTPException(status_code=403, detail="⛔ Доступ заборонено!")

    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as file:
            log_content = file.read().replace("\n", "<br>")

        return f"""
        <!DOCTYPE html>
        <html lang="uk">
        <head>
            <meta charset="UTF-8">
            <title>Лог-файл: {safe_filename}</title>
            <style>
                body {{ font-family: monospace; background: #f4f4f4; margin: 20px; }}
                pre {{ background: white; padding: 20px; border-radius: 8px; box-shadow: 0 0 10px rgba(0,0,0,0.1); }}
            </style>
        </head>
        <body>
            <h2>📜 Лог-файл: {safe_filename}</h2>
            <pre>{log_content}</pre>
            <a href="/logs/">⬅️ Назад до списку логів</a>
        </body>
        </html>
        """
    raise HTTPException(status_code=404, detail="❌ Файл не знайдено")

app.mount("/logs/", StaticFiles(directory=os.path.abspath(LOG_DIR)), name="logs")



@app.post("/XML_prices/google_sheet_to_xml/generate")
def generate():
    threading.Thread(target=lambda: [
        create_xml(str(supplier["Post_ID"]), supplier["Supplier Name"], supplier["Google Sheet ID"], 
                   {"ID": "A", "Name": "B", "Price": "D"})
        for supplier in spreadsheet.worksheet("Sheet1").get_all_records()
    ]).start()
    return {"status": "Генерація XML запущена"}