from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import os
import threading
import gspread
import xml.etree.ElementTree as ET
import json
import time
import re
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import HTMLResponse


# Конфігурація
MASTER_SHEET_ID = "1z16Xcj_58R2Z-JGOMuyx4GpVdQqDn1UtQirCxOrE_hc"
XML_DIR = "/output"



# Перевірка, чи існує папка, і створення, якщо її немає
if not os.path.exists(XML_DIR):
    os.makedirs(XML_DIR)
    print(f"📂 Створено папку для збереження XML: {XML_DIR}")



GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
TOKEN_JSON = os.getenv("TOKEN_JSON")

if not GOOGLE_CREDENTIALS or not TOKEN_JSON:
    raise ValueError("❌ Помилка! Змінні середовища GOOGLE_CREDENTIALS або TOKEN_JSON відсутні!")
try:
    TOKEN_FILE = json.loads(TOKEN_JSON)
    CREDENTIALS_FILE = json.loads(GOOGLE_CREDENTIALS)
except json.JSONDecodeError as e:
    raise ValueError(f"❌ Помилка парсингу JSON: {e}")

app = FastAPI(
    title="Google Sheets to XML API",
    description="Автоматична генерація XML-файлів з Google Sheets",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# Ініціалізація шаблонів
templates = Jinja2Templates(directory="templates")

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


app.mount("/output", StaticFiles(directory=XML_DIR, html=True), name="output")



process_status = {"running": False, "last_update": "", "files_created": 0}


# Авторизація в Google Sheets
from google.auth.transport.requests import Request

def get_google_client():
    creds = None

    if TOKEN_FILE:
        creds = Credentials.from_authorized_user_info(TOKEN_FILE)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())  # Використовуємо правильний об'єкт Request
            print("🔄 Токен оновлено")
        else:
            flow = InstalledAppFlow.from_client_config(
                CREDENTIALS_FILE,
                ["https://www.googleapis.com/auth/spreadsheets"]
            )
            creds = flow.run_local_server(port=8080)
            print("✅ Новий токен отримано")

    return gspread.authorize(creds)




client = get_google_client()
spreadsheet = client.open_by_key(MASTER_SHEET_ID)


# Функція для отримання значень із Google Sheets
def safe_get_value(row, column_letter, default_value="-"):
    try:
        if column_letter and column_letter.isalpha():
            col_index = ord(column_letter.upper()) - 65  # A -> 0, B -> 1, C -> 2 ...
            if len(row) > col_index:
                value = str(row[col_index]).strip()
                return value if value else default_value
    except Exception as e:
        print(f"⚠️ Помилка отримання значення ({column_letter}): {e}")
    return default_value


# Очищення цін від зайвих символів
def clean_price(value):
    try:
        if not value:
            return "0"
        value = re.sub(r"[^\d,\.]", "", value)
        return value.split(",")[0] if "," in value else value.split(".")[0] if "." in value else value
    except Exception as e:
        print(f"⚠️ Помилка обробки ціни: {value} - {e}")
        return "0"

if not os.path.exists(XML_DIR):
    os.makedirs(XML_DIR)



# Функція для генерації XML
def create_xml(supplier_id, supplier_name, sheet_id, columns):
    xml_file = os.path.join(XML_DIR, f"{supplier_id}.xml")

    if os.path.exists(xml_file):
        print(f"⏭️ Пропускаємо {supplier_name} (XML вже існує)")
        return

    print(f"📥 Обробляємо: {supplier_name} ({sheet_id})")

    try:
        spreadsheet = client.open_by_key(sheet_id)
        sheets = spreadsheet.worksheets()
        combined_data = []

        print(f"🔹 Знайдено {len(sheets)} аркуш(ів) у {supplier_name}")

        for sheet in sheets:
            print(f"   🔄 Обробка аркуша: {sheet.title}")
            time.sleep(2)
            data = sheet.get_all_values()
            if len(data) < 2:
                print(f"   ⚠️ Аркуш {sheet.title} порожній, пропускаємо...")
                continue
            combined_data.extend(data[1:])

        if not combined_data:
            print(f"⚠️ Всі аркуші у {supplier_name} порожні! Пропускаємо XML.")
            return

        root = ET.Element("products")
        for row in combined_data:
            product_id = safe_get_value(row, columns["ID"], None)
            name = safe_get_value(row, columns["Name"], None)
            stock = safe_get_value(row, columns["Stock"], "true")
            price = clean_price(safe_get_value(row, columns["Price"], "0"))
            sku = safe_get_value(row, columns["SKU"], None)
            rrp = clean_price(safe_get_value(row, columns["RRP"], None))
            currency = safe_get_value(row, columns["Currency"], "UAH")

            if not name or price == "0":
                continue

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

        tree = ET.ElementTree(root)
        tree.write(xml_file, encoding="utf-8", xml_declaration=True)
        print(f"✅ XML {xml_file} збережено.")

    except gspread.exceptions.APIError as e:
        print(f"❌ Помилка доступу до таблиці {supplier_name} ({sheet_id})! {e}")


# Функція для запуску генерації XML у фоні
def generate_xml():
    global process_status
    process_status["running"] = True
    process_status["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")
    process_status["files_created"] = 0

    supplier_data = spreadsheet.worksheet("Sheet1").get_all_records()
    for supplier in supplier_data:
        supplier_id = str(supplier["Post_ID"])
        supplier_name = supplier["Supplier Name"]
        sheet_id = supplier["Google Sheet ID"]
        columns = {key: supplier[f"{key} Column"] for key in ["ID", "Name", "Stock", "Price", "SKU", "RRP", "Currency"]}
        create_xml(supplier_id, supplier_name, sheet_id, columns)
        process_status["files_created"] += 1

    process_status["running"] = False
    process_status["last_update"] = time.strftime("%Y-%m-%d %H:%M:%S")


# Головна сторінка
@app.get("/XML_prices/google_sheet_to_xml/")
def home():
    return {"status": "Google Sheet to XML API працює"}


@app.get("/XML_prices/google_sheet_to_xml/status")
def status():
    return {"running": process_status["running"], "message": "FastAPI is working!"}


@app.post("/XML_prices/google_sheet_to_xml/generate")
def generate():
    thread = threading.Thread(target=generate_xml)
    thread.start()
    return {"status": "Генерація XML запущена"}


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
