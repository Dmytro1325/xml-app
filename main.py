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
import hashlib
import asyncio
from datetime import datetime
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest

# 🔹 Конфігурація
MASTER_SHEET_ID = "1z16Xcj_58R2Z-JGOMuyx4GpVdQqDn1UtQirCxOrE_hc"
XML_DIR = "/output"
LOG_DIR = "/app/logs"
DEBUG_LOG_FILE = os.path.join(LOG_DIR, "debug_logs", "debug_log.html")
UPDATE_INTERVAL = 1800  # 30 хвилин
price_hash_cache = {}

# 🔹 Створення директорій
for dir_path in [XML_DIR, os.path.dirname(DEBUG_LOG_FILE)]:
    os.makedirs(dir_path, exist_ok=True)

# 🔹 Функція запису логів (в один файл)
def log_to_file(content):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"[{timestamp}] {content}\n"
    
    with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(log_entry)
    
    print(log_entry.strip())  # Дублюємо в консоль

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
                CREDENTIALS_FILE, ["https://www.googleapis.com/auth/spreadsheets"])
            creds = flow.run_local_server(port=8080)
    return gspread.authorize(creds)

client = get_google_client()
spreadsheet = client.open_by_key(MASTER_SHEET_ID)

# 🔹 Функція генерації XML
def create_xml(supplier_id, supplier_name, sheet_id):
    xml_file = os.path.join(XML_DIR, f"{supplier_id}.xml")
    log_to_file(f"📥 Обробка: {supplier_name} ({sheet_id})")
    
    try:
        spreadsheet = client.open_by_key(sheet_id)
        sheets = spreadsheet.worksheets()
        combined_data = []
        
        for sheet in sheets:
            try:
                data = sheet.get_all_values()
                if len(data) < 2:
                    log_to_file(f"⚠️ Аркуш {sheet.title} порожній")
                    continue
                combined_data.extend(data[1:])
            except gspread.exceptions.APIError as e:
                log_to_file(f"❌ Помилка доступу до аркуша {sheet.title}: {e}")
                continue
        
        if not combined_data:
            log_to_file(f"⚠️ {supplier_name} немає даних")
            return
        
        root = ET.Element("products")
        for row in combined_data:
            product = ET.SubElement(root, "product")
            ET.SubElement(product, "id").text = row[0] if len(row) > 0 else "-"
            ET.SubElement(product, "name").text = row[1] if len(row) > 1 else "-"
            ET.SubElement(product, "price").text = row[3] if len(row) > 3 else "0"
        
        ET.ElementTree(root).write(xml_file, encoding="utf-8", xml_declaration=True)
        log_to_file(f"✅ XML {xml_file} збережено ({len(combined_data)} товарів)")
    
    except gspread.exceptions.APIError as e:
        log_to_file(f"❌ Помилка доступу до {supplier_name}: {e}")

# 🔹 Автоматичне оновлення XML
async def periodic_update():
    while True:
        log_to_file("🔄 [Auto-Update] Початок перевірки змін у Google Sheets...")
        try:
            supplier_data = spreadsheet.worksheet("Sheet1").get_all_records()
        except gspread.exceptions.APIError as e:
            log_to_file(f"❌ Помилка доступу до головної таблиці: {e}")
            await asyncio.sleep(UPDATE_INTERVAL)
            continue

        for supplier in supplier_data:
            create_xml(str(supplier["Post_ID"]), supplier["Supplier Name"], supplier["Google Sheet ID"])

        log_to_file("✅ [Auto-Update] Перевірку завершено, очікуємо наступний цикл...")
        await asyncio.sleep(UPDATE_INTERVAL)

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
    """ Відображаємо останній дебаг-лог у браузері """
    if os.path.exists(DEBUG_LOG_FILE):
        return FileResponse(DEBUG_LOG_FILE)
    raise HTTPException(status_code=404, detail="Файл логів не знайдено.")

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(periodic_update())

@app.post("/XML_prices/google_sheet_to_xml/generate")
def generate():
    threading.Thread(target=lambda: [
        create_xml(str(supplier["Post_ID"]), supplier["Supplier Name"], supplier["Google Sheet ID"])
        for supplier in spreadsheet.worksheet("Sheet1").get_all_records()
    ]).start()
    return {"status": "Генерація XML запущена"}
