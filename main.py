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
import re
import requests
import hashlib
import asyncio
from datetime import datetime
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request as GoogleRequest

# Конфігурація
MASTER_SHEET_ID = "1z16Xcj_58R2Z-JGOMuyx4GpVdQqDn1UtQirCxOrE_hc"
XML_DIR = "/app/output"
UPDATE_INTERVAL = 1800  # 30 хвилин
SUCCESS_LOG_DIR = "/app/logs/success_logs"
ERROR_LOG_DIR = "/app/logs/error_logs"
price_hash_cache = {}

# Створення директорій
for dir_path in [XML_DIR, SUCCESS_LOG_DIR, ERROR_LOG_DIR]:
    os.makedirs(dir_path, exist_ok=True)

# Авторизація Google Sheets
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
            print("🔄 Токен оновлено")
        else:
            flow = InstalledAppFlow.from_client_config(
                CREDENTIALS_FILE, ["https://www.googleapis.com/auth/spreadsheets"])
            creds = flow.run_local_server(port=8080)
    return gspread.authorize(creds)

client = get_google_client()
spreadsheet = client.open_by_key(MASTER_SHEET_ID)

# Функція логування
def log_to_file(log_type, content):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_dir = SUCCESS_LOG_DIR if log_type == "success" else ERROR_LOG_DIR
    log_file = os.path.join(log_dir, f"{timestamp}.html" if log_type == "success" else f"{timestamp}_error.html")
    with open(log_file, "w", encoding="utf-8") as f:
        f.write("<html><body><pre>\n" + content + "\n</pre></body></html>")
    return log_file

# Функція генерації XML
def create_xml(supplier_id, supplier_name, sheet_id):
    xml_file = os.path.join(XML_DIR, f"{supplier_id}.xml")
    log_content = f"📥 Обробка: {supplier_name} ({sheet_id})\n"
    try:
        spreadsheet = client.open_by_key(sheet_id)
        sheets = spreadsheet.worksheets()
        combined_data = []
        for sheet in sheets:
            data = sheet.get_all_values()
            if len(data) < 2:
                log_content += f"⚠️ Аркуш {sheet.title} порожній\n"
                continue
            combined_data.extend(data[1:])
        if not combined_data:
            log_content += f"⚠️ {supplier_name} немає даних\n"
            log_to_file("error", log_content)
            return
        root = ET.Element("products")
        for row in combined_data:
            product = ET.SubElement(root, "product")
            ET.SubElement(product, "id").text = row[0] if len(row) > 0 else "-"
            ET.SubElement(product, "name").text = row[1] if len(row) > 1 else "-"
            ET.SubElement(product, "price").text = row[3] if len(row) > 3 else "0"
        ET.ElementTree(root).write(xml_file, encoding="utf-8", xml_declaration=True)
        log_content += f"✅ XML {xml_file} збережено ({len(combined_data)} товарів)\n"
        log_to_file("success", log_content)
    except gspread.exceptions.APIError as e:
        log_content += f"❌ Помилка доступу до {supplier_name}: {e}\n"
        log_to_file("error", log_content)

# Автоматичне оновлення XML
async def periodic_update():
    while True:
        supplier_data = spreadsheet.worksheet("Sheet1").get_all_records()
        for supplier in supplier_data:
            create_xml(str(supplier["Post_ID"]), supplier["Supplier Name"], supplier["Google Sheet ID"])
        await asyncio.sleep(UPDATE_INTERVAL)

# API
app = FastAPI(
    title="Google Sheets to XML API",
    description="Автоматична генерація XML-файлів з Google Sheets",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)
  
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

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(periodic_update())




@app.get("/logs/", response_class=HTMLResponse)
def list_logs(request: Request):
    logs = []
    for log_dir, log_type in [(SUCCESS_LOG_DIR, "success"), (ERROR_LOG_DIR, "error")]:
        for filename in os.listdir(log_dir):
            logs.append({"name": filename, "size": os.path.getsize(os.path.join(log_dir, filename)), "type": log_type})
    return templates.TemplateResponse("logs.html", {"request": request, "logs": logs})

@app.get("/logs/view/{filename}")
def view_log(filename: str):
    for log_dir in [SUCCESS_LOG_DIR, ERROR_LOG_DIR]:
        file_path = os.path.join(log_dir, filename)
        if os.path.exists(file_path):
            return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="Файл не знайдено")

@app.post("/XML_prices/google_sheet_to_xml/generate")
def generate():
    thread = threading.Thread(target=lambda: [create_xml(str(supplier["Post_ID"]), supplier["Supplier Name"], supplier["Google Sheet ID"]) for supplier in spreadsheet.worksheet("Sheet1").get_all_records()])
    thread.start()
    return {"status": "Генерація XML запущена"}


@app.get("/XML_prices/google_sheet_to_xml/status")
def status():
    return {"running": process_status["running"], "message": "FastAPI is working!"}



@app.get("/XML_prices/google_sheet_to_xml/files")
def list_files():
    files = [f for f in os.listdir(XML_DIR) if f.endswith(".xml")]
    return {"files": files}


@app.get("/XML_prices/google_sheet_to_xml/download/{filename}")
def download_file(filename: str):
    file_path = os.path.join(XML_DIR, filename)
    if os.path.exists(file_path):
        return FileResponse(file_path, filename=filename)
    raise HTTPException(status_code=404, detail="Файл не знайдено")

@app.delete("/XML_prices/google_sheet_to_xml/delete/{filename}")
def delete_file(filename: str):
    """
    Видаляє конкретний файл у папці /output/
    """
    file_path = os.path.join(XML_DIR, filename)
    if os.path.exists(file_path):
        os.remove(file_path)
        return {"status": "success", "message": f"Файл {filename} видалено."}
    else:
        raise HTTPException(status_code=404, detail=f"Файл {filename} не знайдено.")


@app.delete("/XML_prices/google_sheet_to_xml/delete_all")
def delete_all_files():
    """
    Видаляє всі файли у папці /output/
    """
    files = os.listdir(XML_DIR)
    if not files:
        return {"status": "success", "message": "Папка вже порожня."}

    for file in files:
        file_path = os.path.join(XML_DIR, file)
        os.remove(file_path)

    return {"status": "success", "message": "Всі файли у папці output видалено."}