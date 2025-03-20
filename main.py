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
import requests
import hashlib
import asyncio
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from fastapi.templating import Jinja2Templates
from fastapi.requests import Request
from fastapi.responses import HTMLResponse
from fastapi import Depends
from google.auth.transport.requests import Request as GoogleRequest

# Конфігурація
MASTER_SHEET_ID = "1z16Xcj_58R2Z-JGOMuyx4GpVdQqDn1UtQirCxOrE_hc"
XML_DIR = "/output"
UPDATE_INTERVAL = 1800  # Оновлення кожні 30 хвилин (1800 секунд)
price_hash_cache = {}  # Кеш для збереження хешів файлів


# Перевірка, чи існує папка
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


app.mount("/output", StaticFiles(directory=XML_DIR, html=True), name="output")


process_status = {"running": False, "last_update": "", "files_created": 0}

# Авторизація в Google Sheets
def get_google_client():
    creds = None

    if TOKEN_FILE:
        creds = Credentials.from_authorized_user_info(TOKEN_FILE)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            session = requests.Session()
            creds.refresh(GoogleRequest(session))
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

# Функція для отримання хешу таблиці
def get_price_hash(sheet):
    try:
        data = sheet.get_all_values()
        data_str = json.dumps(data, ensure_ascii=False)
        return hashlib.md5(data_str.encode()).hexdigest()
    except Exception as e:
        print(f"⚠️ Помилка отримання даних: {e}")
        return None

# Функція генерації XML
def create_xml(supplier_id, supplier_name, sheet_id, columns):
    xml_file = os.path.join(XML_DIR, f"{supplier_id}.xml")

    print(f"📥 Обробка: {supplier_name} ({sheet_id})")

    try:
        spreadsheet = client.open_by_key(sheet_id)
        sheets = spreadsheet.worksheets()
        combined_data = []

        for sheet in sheets:
            data = sheet.get_all_values()
            if len(data) < 2:
                continue
            combined_data.extend(data[1:])

        if not combined_data:
            print(f"⚠️ У {supplier_name} немає даних! Пропускаємо XML.")
            return

        root = ET.Element("products")
        for row in combined_data:
            product_id = row[0] if len(row) > 0 else "-"
            name = row[1] if len(row) > 1 else "-"
            stock = row[2] if len(row) > 2 else "true"
            price = row[3] if len(row) > 3 else "0"
            sku = row[4] if len(row) > 4 else "-"
            currency = row[5] if len(row) > 5 else "UAH"

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

        tree = ET.ElementTree(root)
        tree.write(xml_file, encoding="utf-8", xml_declaration=True)
        print(f"✅ XML {xml_file} збережено.")

    except gspread.exceptions.APIError as e:
        print(f"❌ Помилка доступу до {supplier_name} ({sheet_id}): {e}")

# Автоматичне оновлення XML
async def periodic_update():
    """
    Фоновий процес, який оновлює тільки ті XML-файли, які змінилися,
    з урахуванням кешу та обмеження на запити.
    """
    while True:
        print("🔄 [Auto-Update] Починаємо перевірку змін у Google Sheets...")

        supplier_data = spreadsheet.worksheet("Sheet1").get_all_records()
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
                    print(f"⚠️ {supplier_name}: Пропускаємо, бо в попередньому циклі було перевищено ліміт API.")
                    continue

                try:
                    sheet = client.open_by_key(sheet_id).sheet1
                    time.sleep(5)  # Збільшуємо паузу між запитами

                    new_hash = get_price_hash(sheet)

                    if supplier_id in price_hash_cache and price_hash_cache[supplier_id] == new_hash:
                        print(f"✅ {supplier_name}: Дані не змінилися, XML не оновлюємо")
                    else:
                        print(f"🔄 {supplier_name}: Дані змінилися, оновлюємо XML")
                        price_hash_cache[supplier_id] = new_hash
                        create_xml(supplier_id, supplier_name, sheet_id, supplier)
                        updated_suppliers.append(supplier_name)

                except gspread.exceptions.APIError as e:
                    if "429" in str(e):
                        print(f"⚠️ Ліміт запитів вичерпано для {supplier_name}. Чекаємо 60 сек...")
                        time.sleep(60)  # Чекаємо довше перед повторною спробою
                        skipped_suppliers.append(supplier_id)  # Пропускаємо його до наступного циклу
                    else:
                        print(f"❌ Помилка обробки {supplier_name}: {e}")

            print("⏳ Чекаємо 10 секунд перед наступною групою постачальників...")
            await asyncio.sleep(10)  # Робимо паузу між групами

        print("✅ [Auto-Update] Перевірку завершено, чекаємо на наступний цикл...")
        await asyncio.sleep(UPDATE_INTERVAL)  # Чекаємо 30 хвилин до наступного оновлення


@app.on_event("startup")
async def startup_event():
    asyncio.create_task(periodic_update())

@app.post("/XML_prices/google_sheet_to_xml/generate")
@app.post("/XML_prices/google_sheet_to_xml/generate")
def generate():
    """
    Ручний запуск генерації XML-файлів. Оптимізовано для уникнення ліміту Google API.
    """
    def process_generation():
        global process_status
        print("🔄 [Manual Update] Запущено ручне оновлення XML...")

        supplier_data = spreadsheet.worksheet("Sheet1").get_all_records()
        updated_suppliers = []

        for supplier in supplier_data:
            supplier_id = str(supplier["Post_ID"])
            supplier_name = supplier["Supplier Name"]
            sheet_id = supplier["Google Sheet ID"]

            try:
                sheet = client.open_by_key(sheet_id).sheet1
                time.sleep(5)  # Запобігаємо перевищенню ліміту запитів

                new_hash = get_price_hash(sheet)

                if supplier_id in price_hash_cache and price_hash_cache[supplier_id] == new_hash:
                    print(f"✅ {supplier_name}: Дані не змінилися, XML не оновлюємо")
                else:
                    print(f"🔄 {supplier_name}: Дані змінилися, оновлюємо XML")
                    price_hash_cache[supplier_id] = new_hash
                    create_xml(supplier_id, supplier_name, sheet_id, supplier)
                    updated_suppliers.append(supplier_name)

            except gspread.exceptions.APIError as e:
                if "429" in str(e):
                    print(f"⚠️ Ліміт запитів вичерпано для {supplier_name}. Чекаємо 60 сек...")
                    time.sleep(60)  # Уникаємо блокування API
                    continue
                else:
                    print(f"❌ Помилка обробки {supplier_name}: {e}")

        if updated_suppliers:
            print(f"✅ Оновлено XML для: {', '.join(updated_suppliers)}")
        else:
            print("✅ Жодних змін не знайдено, оновлення не потрібне.")

        print("🔄 [Manual Update] Ручне оновлення завершено.")

    # Запускаємо процес генерації у окремому потоці, щоб не блокувати сервер
    thread = threading.Thread(target=process_generation)
    thread.start()
    
    return {"status": "Генерація XML запущена у фоновому режимі"}

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