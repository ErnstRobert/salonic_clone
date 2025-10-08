# app.py
import streamlit as st
from datetime import datetime, date, time, timedelta
import pandas as pd
import json
import io

# --- Google Sheets libs ---
import gspread
from google.oauth2.service_account import Credentials

# ----------------------
st.set_page_config(page_title="Eszter Salonic-klón", layout="centered")

# ----------------------
# CONFIG (szerkeszthető)
SHEET_NAME = "Eszter_Salonic"
BOOKINGS_WS = "Bookings"
SERVICES_WS = "Services"
WORKING_HOURS = (time(9, 0), time(18, 0))  # 09:00 - 18:00
SLOT_MINUTES = 30  # alap időslot méret (per szolgáltatás lehet hosszabb)
# ----------------------

# --- Helper: Google Sheets client from Streamlit secrets ---
# Expected secrets formats:
# 1) Preferred: a table/dict named 'gcp_service_account' in Streamlit secrets
#    Example in .streamlit/secrets.toml:
#    [gcp_service_account]
#    type = "service_account"
#    project_id = "..."
#    private_key_id = "..."
#    private_key = "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----\n"
#    client_email = "..."
#
# 2) Fallback: a single JSON string under key 'GCP_SA_JSON' (can contain literal "\\n" sequences)
#    In that case set GCP_SA_JSON = '{"type":"service_account", ... }'
#
# The helper below normalizes escaped newlines ("\\n") into real newlines and validates
# the presence of required fields, giving clearer Streamlit errors if something is missing.
@st.cache_resource(ttl=600)
def get_gsheets_client():
    info = None

    # 1) Preferált: TOML / dict táblázat in secrets under key 'gcp_service_account'
    if "gcp_service_account" in st.secrets:
        try:
            info = dict(st.secrets["gcp_service_account"])
        except Exception:
            st.error("A 'gcp_service_account' formátuma érvénytelen a Secrets-ben. Legyen egy kulcs-érték párokból álló táblázat.")
            st.stop()

    # 2) Fallback: JSON string in GCP_SA_JSON
    if info is None and "GCP_SA_JSON" in st.secrets:
        raw = st.secrets["GCP_SA_JSON"]
        # If secret was pasted as a Python-style triple-quoted JSON, strip surrounding whitespace
        raw = raw.strip()
        try:
            info = json.loads(raw)
        except Exception as e:
            # Try a common case: user pasted JSON and newlines were escaped (\n)
            try:
                info = json.loads(raw.replace('\\\n', '\\n'))
            except Exception:
                st.error("A GCP_SA_JSON nem érvényes JSON. Használd inkább a 'gcp_service_account' táblát a Secrets-ben, vagy ellenőrizd a JSON-t.")
                st.stop()

    if info is None:
        st.error("Hiányzik a Google service account a Secrets-ben. Adj meg 'gcp_service_account' táblát VAGY GCP_SA_JSON-t.")
        st.stop()

    # validate minimal required keys
    required = ["type", "project_id", "private_key_id", "private_key", "client_email"]
    missing = [k for k in required if k not in info or not info.get(k)]
    if missing:
        st.error(f"A service account JSON hiányos, hiányzó mezők: {', '.join(missing)}")
        st.stop()

    pk = info.get("private_key", "")
    # Normalizálás: ha valaki escaped newlines-szal (\n) adta meg, alakítsuk valódi sorokra
    # Accept either real newlines or literal '\n' sequences
    if isinstance(pk, str) and "\\n" in pk and "BEGIN PRIVATE KEY" in pk:
        pk = pk.replace("\\n", "\n")
    pk = pk.strip()
    info["private_key"] = pk

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    try:
        creds = Credentials.from_service_account_info(info, scopes=scopes)
        client = gspread.authorize(creds)
    except Exception as e:
        st.error(f"Hiba a Google hitelesítés létrehozásakor: {e}")
        st.stop()
    return client


# --- Initialize sheet/workbooks if not existing
def ensure_sheets():
    client = get_gsheets_client()
    try:
        sh = client.open(SHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sh = client.create(SHEET_NAME)
        # Share is not necessary if using service account that owns it,
        # but you might want to share with Eszter's email manually if needed.
    # ensure worksheets
    try:
        bookings_ws = sh.worksheet(BOOKINGS_WS)
    except gspread.WorksheetNotFound:
        bookings_ws = sh.add_worksheet(title=BOOKINGS_WS, rows="1000", cols="20")
        bookings_ws.append_row(["id","date","start_time","end_time","service","duration_min","name","phone","status","note","created_at"])
    try:
        services_ws = sh.worksheet(SERVICES_WS)
    except gspread.WorksheetNotFound:
        services_ws = sh.add_worksheet(title=SERVICES_WS, rows="50", cols="10")
        # default services (name, duration_min, price)
        default_services = [
            ["Géllakk", 60, "8000"],
            ["Töltés", 90, "12000"],
            ["Manikűr", 45, "6000"],
            ["Díszítés (1db)", 10, "500"]
        ]
        services_ws.append_row(["service","duration_min","price"])
        for r in default_services:
            services_ws.append_row(r)
    return sh

# --- Read bookings and services into DataFrames
@st.cache_data(ttl=30)
def read_dataframes():
    sh = ensure_sheets()
    bookings_ws = sh.worksheet(BOOKINGS_WS)
    services_ws = sh.worksheet(SERVICES_WS)
    bookings = pd.DataFrame(bookings_ws.get_all_records())
    services = pd.DataFrame(services_ws.get_all_records())
    return bookings, services, sh

# --- Utility: generate timeslots for a selected day based on working hours and services
def generate_slots(selected_date: date, services_df: pd.DataFrame, bookings_df: pd.DataFrame):
    slots = []
    start_dt = datetime.combine(selected_date, WORKING_HOURS[0])
    end_dt = datetime.combine(selected_date, WORKING_HOURS[1])
    # We will present slots in SLOT_MINUTES increments, but booking duration depends on selected service
    cur = start_dt
    while cur + timedelta(minutes=15) <= end_dt:  # minimal visibility step
        slots.append(cur.time().strftime("%H:%M"))
        cur += timedelta(minutes=SLOT_MINUTES)
    # filter out slots where any existing booking overlaps (we'll do overlap check when user picks service)
    return slots

def overlaps(start_a, end_a, start_b, end_b):
    return (start_a < end_b) and (end_a > start_b)

# --- Append booking to sheet
def save_booking(sh, booking_record: dict):
    ws = sh.worksheet(BOOKINGS_WS)
    # generate a simple id
    booking_id = int(datetime.utcnow().timestamp() * 1000)
    row = [
        booking_id,
        booking_record["date"],
        booking_record["start_time"],
        booking_record["end_time"],
        booking_record["service"],
        booking_record["duration_min"],
        booking_record["name"],
        booking_record["phone"],
        "booked",
        booking_record.get("note",""),
        datetime.utcnow().isoformat()
    ]
    ws.append_row(row)
    return booking_id

# ----------------------
# UI
st.title("Eszter — Vendégfoglaló")

bookings_df, services_df, sh = read_dataframes()

tabs = st.tabs(["Foglalás (vendég)", "Admin (Eszter)"])
with tabs[0]:
    st.header("Foglalj időpontot")
    # services dropdown
    services_options = services_df["service"].tolist()
    service_sel = st.selectbox("Szolgáltatás", services_options)
    service_row = services_df[services_df["service"] == service_sel].iloc[0]
    duration_min = int(service_row["duration_min"])
    price = service_row.get("price", "")

    st.write(f"Időtartam: **{duration_min}** perc — Ár: **{price} HUF**")
    # date picker
    selected_date = st.date_input("Dátum", min_value=date.today())
    # timeslots
    slot_list = generate_slots(selected_date, services_df, bookings_df)

    # Filter out slots that overlap existing bookings of that day
    day_bookings = bookings_df[bookings_df["date"] == selected_date.isoformat()] if not bookings_df.empty else pd.DataFrame()
    avail_slots = []
    for s in slot_list:
        stime = datetime.combine(selected_date, datetime.strptime(s, "%H:%M").time())
        etime = stime + timedelta(minutes=duration_min)
        conflict = False
        if not day_bookings.empty:
            for idx, rb in day_bookings.iterrows():
                try:
                    b_start = datetime.combine(selected_date, datetime.strptime(rb["start_time"], "%H:%M").time())
                    b_end = datetime.combine(selected_date, datetime.strptime(rb["end_time"], "%H:%M").time())
                except Exception:
                    # if parsing fail, skip
                    continue
                if overlaps(stime, etime, b_start, b_end):
                    conflict = True
                    break
        if not conflict and etime.time() <= WORKING_HOURS[1]:
            avail_slots.append(s)

    if not avail_slots:
        st.info("Sajnos ezen a napon nincs szabad időpont (válassz másik napot vagy szolgáltatást).")
    else:
        selected_time = st.selectbox("Válassz időpontot", avail_slots)
        name = st.text_input("Név")
        phone = st.text_input("Telefonszám")
        note = st.text_area("Megjegyzés (opcionális)")

        if st.button("Foglalás megerősítése"):
            if not name.strip() or not phone.strip():
                st.warning("Add meg a nevet és a telefonszámot.")
            else:
                booking = {
                    "date": selected_date.isoformat(),
                    "start_time": selected_time,
                    "duration_min": duration_min,
                    "end_time": (datetime.combine(selected_date, datetime.strptime(selected_time, "%H:%M").time()) + timedelta(minutes=duration_min)).time().strftime("%H:%M"),
                    "service": service_sel,
                    "name": name.strip(),
                    "phone": phone.strip(),
                    "note": note.strip()
                }
                booking_id = save_booking(sh, booking)
                st.success(f"Foglalás rögzítve! Azonosító: {booking_id}")
                st.balloons()
                st.write("Foglalás részletei:")
                st.json(booking)

with tabs[1]:
    st.header("Admin panel")
    # simple password protection
    admin_pw = st.secrets.get("ADMIN_PASSWORD", None)
    if not admin_pw:
        st.warning("Nincs admin jelszó beállítva a Streamlit secrets-ben (ADMIN_PASSWORD). Átmeneti üzemmódban az admin panel nem érhető el.")
    password = st.text_input("Admin jelszó", type="password")
    if password and admin_pw and password == admin_pw:
        st.success("Beléptél — üdv Eszter!")
        # Admin controls
        st.subheader("Foglalások (ma és a közeljövő)")
        bookings_df = pd.DataFrame(sh.worksheet(BOOKINGS_WS).get_all_records())
        if bookings_df.empty:
            st.info("Nincsenek foglalások.")
        else:
            # convert date for sorting
            try:
                bookings_df['date_dt'] = pd.to_datetime(bookings_df['date'])
                bookings_df = bookings_df.sort_values(['date_dt','start_time'])
            except:
                pass
            st.dataframe(bookings_df[['id','date','start_time','end_time','service','name','phone','status']].head(200))

            # allow admin to cancel a booking
            st.subheader("Törlés / státusz módosítás")
            booking_id_to_change = st.text_input("Add meg a booking id-t törléshez/módosításhoz")
            new_status = st.selectbox("Új státusz", ["booked","cancelled","done"])
            if st.button("Alkalmaz státusz"):
                if not booking_id_to_change.strip():
                    st.warning("Adj meg egy id-t.")
                else:
                    ws = sh.worksheet(BOOKINGS_WS)
                    rows = ws.get_all_records()
                    updated = False
                    for i, r in enumerate(rows, start=2):  # header row = 1
                        if str(r.get("id")) == booking_id_to_change.strip():
                            # update cell (status is column 9 per header)
                            # find header index
                            headers = ws.row_values(1)
                            try:
                                status_col = headers.index("status") + 1
                                ws.update_cell(i, status_col, new_status)
                                updated = True
                                st.success("Frissítve.")
                                break
                            except ValueError:
                                st.error("A táblában nincs 'status' mező.")
                    if not updated:
                        st.error("Nem található ilyen id.")
        # Services editor
        st.subheader("Szolgáltatások szerkesztése")
        services_df = pd.DataFrame(sh.worksheet(SERVICES_WS).get_all_records())
        st.dataframe(services_df)
        with st.form("add_service"):
            new_service = st.text_input("Szolgáltatás neve")
            new_duration = st.number_input("Időtartam (perc)", min_value=5, max_value=480, value=60)
            new_price = st.text_input("Ár (HUF)", value="0")
            if st.form_submit_button("Hozzáad"):
                if new_service.strip():
                    ws = sh.worksheet(SERVICES_WS)
                    ws.append_row([new_service.strip(), int(new_duration), new_price.strip()])
                    st.success("Szolgáltatás hozzáadva. Frissítsd az oldalt, hogy lásd a változást.")
    else:
        if password:
            st.error("Rossz jelszó.")
        st.info("Adj meg jelszót, hogy belépj az admin felületre.")

st.markdown("---")
st.caption("Készült: Salonic-klón prototípus • Használat: Eszter (műkörmös).")
