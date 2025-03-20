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

# 🔹 Конфігурація
MASTER_SHEET_ID = "1z16Xcj_58R2Z-JGOMuyx4GpVdQqDn1UtQirCxOrE_hc"
XML_DIR = "/output"
LOG_DIR = "/app/logs"
DEBUG_LOG_FILE = os.path.join(LOG_DIR, "debug_logs", "debug_log.html")
UPDATE_INTERVAL = 1800  # 30 хвилин

# 🔹 Створення директорій
for dir_path in [XML_DIR, os.path.dirname(DEBUG_LOG_FILE)]:
    os.makedirs(dir_path, exist_ok=True)

# 🔹 Функція логування
def log_to_file(content):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {content}\n"

    with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_entry)

    print(log_entry.strip())  # Виводимо в консоль

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
    try:
        if column_letter and column_letter.isalpha():
            col_index = ord(column_letter.upper()) - 65  # A -> 0, B -> 1, C -> 2
            if len(row) > col_index:
                value = str(row[col_index]).strip()
                return value if value else default_value
    except Exception as e:
        log_to_file(f"⚠️ Помилка при отриманні значення ({column_letter}): {e}")

    return default_value

# 🔹 Функція для очищення ціни
def clean_price(value):
    try:
        if not value:
            return "0"
        value = re.sub(r"[^\d,\.]", "", value)
        if "," in value:
            value = value.split(",")[0]
        elif "." in value:
            value = value.split(".")[0]
        return value if value else "0"
    except Exception as e:
        log_to_file(f"⚠️ Помилка обробки ціни: {value} - {e}")
        return "0"

# 🔹 Функція генерації XML
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
                combined_data.extend(data[1:])

            if not combined_data:
                log_to_file(f"⚠️ {supplier_name} немає даних")
                return

            root = ET.Element("products")
            for row in combined_data:
                product = ET.SubElement(root, "product")
                ET.SubElement(product, "id").text = safe_get_value(row, columns["ID"], "-")
                ET.SubElement(product, "name").text = safe_get_value(row, columns["Name"], "-")
                ET.SubElement(product, "price").text = clean_price(safe_get_value(row, columns["Price"], "0"))

            ET.ElementTree(root).write(xml_file, encoding="utf-8", xml_declaration=True)
            log_to_file(f"✅ XML {xml_file} збережено ({len(combined_data)} товарів)")
            time.sleep(random.uniform(1.5, 2.5))
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

async def periodic_update():
    """
    Фоновий процес, який оновлює тільки ті XML-файли, які змінилися,
    з урахуванням кешу та обмеження на запити.
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

                try:
                    sheet = client.open_by_key(sheet_id).sheet1
                    await asyncio.sleep(5)  # Запобігаємо перевантаженню API
                    
                    new_hash = get_price_hash(sheet)

                    if supplier_id in price_hash_cache and price_hash_cache[supplier_id] == new_hash:
                        log_to_file(f"⏭️ {supplier_name}: Немає змін, пропускаємо...")
                        continue

                    price_hash_cache[supplier_id] = new_hash  # Оновлюємо кеш
                    create_xml(supplier_id, supplier_name, sheet_id, {"ID": "A", "Name": "B", "Price": "D"})
                    updated_suppliers.append(supplier_name)

                except gspread.exceptions.APIError as e:
                    if "429" in str(e):
                        log_to_file(f"⚠️ Ліміт запитів вичерпано для {supplier_name}. Чекаємо 60 сек...")
                        await asyncio.sleep(60)  # Чекаємо довше перед повторною спробою
                        skipped_suppliers.append(supplier_id)  # Пропускаємо до наступного циклу
                    else:
                        log_to_file(f"❌ Помилка обробки {supplier_name}: {e}")

        log_to_file(f"✅ [Auto-Update] Оновлено {len(updated_suppliers)} постачальників, чекаємо на наступний цикл...")
        await asyncio.sleep(UPDATE_INTERVAL)  # Чекаємо 30 хвилин до наступного оновлення


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




@app.post("/XML_prices/google_sheet_to_xml/generate")
def generate():
    threading.Thread(target=lambda: [
        create_xml(str(supplier["Post_ID"]), supplier["Supplier Name"], supplier["Google Sheet ID"], 
                   {"ID": "A", "Name": "B", "Price": "D"})
        for supplier in spreadsheet.worksheet("Sheet1").get_all_records()
    ]).start()
    return {"status": "Генерація XML запущена"}