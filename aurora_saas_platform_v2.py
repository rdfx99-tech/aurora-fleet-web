import os
import streamlit as st
import datetime
import requests
import uuid
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh
import streamlit.components.v1 as components
from sqlalchemy import create_engine, text
import PIL.Image
import io
from google import genai

# ==========================================
# ⚙️ 1. SECURITY & DATABASE CONNECTION
# ==========================================
# ดึงค่าจากระบบ Render โดยตรง (ห้ามแปะรหัสจริงในไฟล์นี้เด็ดขาด)
DB_URL = os.getenv("DATABASE_URL")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
SLIP_CHECK_API_KEY = os.getenv("SLIP_CHECK_API_KEY")

engine = create_engine(DB_URL)

def init_db():
    with engine.connect() as conn:
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY, password TEXT, role TEXT, 
                expire_date DATE, tier TEXT, tele_token TEXT DEFAULT '', tele_chat_id TEXT DEFAULT ''
            )'''))
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS payments (
                id SERIAL PRIMARY KEY, username TEXT, timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status TEXT
            )'''))
        conn.execute(text('''
            CREATE TABLE IF NOT EXISTS api_keys (
                api_key TEXT PRIMARY KEY, username TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, status TEXT
            )'''))
        
        res = conn.execute(text("SELECT username FROM users WHERE username='P_S'")).fetchone()
        if not res:
            # ✅ ใช้ตัวแปรส่วนกลางที่ดึงมาจาก Render (ปลอดภัย ไร้รอยรั่ว)
            conn.execute(text("""
                INSERT INTO users (username, password, role, expire_date, tier, tele_token, tele_chat_id) 
                VALUES ('P_S', 'aurora', 'admin', '2099-12-31', 'SUPER_ADMIN', :t, :c)
            """), {
                "t": TELEGRAM_BOT_TOKEN, 
                "c": TELEGRAM_CHAT_ID
            })
        conn.commit()

init_db()

# ==========================================
# 🧠 2. SAAS LOGIC (PostgreSQL ONLY)
# ==========================================
def register_user(username, password):
    with engine.connect() as conn:
        try:
            expire = datetime.date.today() + datetime.timedelta(days=30)
            conn.execute(text("INSERT INTO users (username, password, role, expire_date, tier, tele_token, tele_chat_id) VALUES (:u, :p, 'user', :e, 'FREE_TRIAL', '', '')"),
                         {"u": username, "p": password, "e": expire})
            conn.commit()
            return True
        except: return False

def login_user(username, password):
    with engine.connect() as conn:
        return conn.execute(text("SELECT role, expire_date, tier, tele_token, tele_chat_id FROM users WHERE username=:u AND password=:p"),
                            {"u": username, "p": password}).fetchone()

def generate_api_key(username):
    new_key = f"aurora_live_{uuid.uuid4().hex}"
    with engine.connect() as conn:
        conn.execute(text("INSERT INTO api_keys (api_key, username, status) VALUES (:k, :u, 'ACTIVE')"),
                     {"k": new_key, "u": username})
        conn.commit()
    return new_key

def send_telegram_alert(message):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage", json={"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"})

def send_telegram_slip(username, slip_bytes):
    data = {'chat_id': TELEGRAM_CHAT_ID, 'caption': f"🚨 <b>แจ้งเตือนชำระเงิน!</b>\n👤 User: <code>{username}</code>\n💎 ร้องขออัปเกรดเป็น <b>VIP_USER_PRO</b>", 'parse_mode': 'HTML'}
    res = requests.post(f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendPhoto", files={'photo': slip_bytes}, data=data)
    return res.status_code == 200
# ==========================================
# 📡 2. DATA ENGINES (สำหรับระบบรถ Fleet)
# ==========================================
@st.cache_data(ttl=600)
def fetch_nasa_hazards():
    try:
        url = "https://eonet.gsfc.nasa.gov/api/v3/events?status=open&days=7"
        events = requests.get(url, timeout=10).json().get('events', [])
        hazards = []
        for event in events:
            if event['categories'][0]['id'] in ['severeStorms', 'wildfires', 'volcanoes']:
                coords = event['geometry'][0]['coordinates']
                if isinstance(coords[0], float): 
                    hazards.append({"Title": event['title'], "Category": event['categories'][0]['title'], "Lat": coords[1], "Lon": coords[0]})
        return pd.DataFrame(hazards)
    except: return pd.DataFrame()

@st.cache_data(ttl=300)
def fetch_advanced_weather(lat, lon):
    try:
        w_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,rain,wind_speed_10m"
        w_res = requests.get(w_url, timeout=5).json().get("current", {})
        
        a_url = f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&current=pm2_5"
        a_res = requests.get(a_url, timeout=5).json().get("current", {})
        
        return w_res.get("temperature_2m", 0), w_res.get("rain", 0), w_res.get("wind_speed_10m", 0), a_res.get("pm2_5", 0)
    except: return 0, 0, 0, 0

def get_simulated_fleet():
    base_lats, base_lons = [13.75, 12.65, 14.97, 16.48, 13.36], [100.50, 101.25, 102.10, 104.28, 100.98]
    noise = np.random.normal(0, 0.05, 5)
    fleet_data = []
    for i in range(5):
        lat, lon = base_lats[i] + noise[i], base_lons[i] + noise[i]
        temp, rain, wind, pm25 = fetch_advanced_weather(lat, lon)
        fleet_data.append({
            "Vehicle": f"TRUCK-00{i+1}", "Lat": lat, "Lon": lon, "Speed (km/h)": np.random.randint(60, 95),
            "Temp (°C)": temp, "Rain (mm)": rain, "Wind (km/h)": wind, "PM 2.5": pm25, "Status": "Moving"
        })
    return pd.DataFrame(fleet_data)

def calculate_risks(df_fleet, df_hazards):
    alerts = []
    if not df_hazards.empty:
        for _, truck in df_fleet.iterrows():
            for _, hazard in df_hazards.iterrows():
                if np.sqrt((truck['Lat'] - hazard['Lat'])**2 + (truck['Lon'] - hazard['Lon'])**2) < 0.5:
                    alerts.append({"Vehicle": truck['Vehicle'], "Hazard": hazard['Title'], "Type": hazard['Category'], "Message": f"🚨 เข้าใกล้โซนอันตราย! ({hazard['Category']})"})
            if truck['Rain (mm)'] > 5.0: alerts.append({"Vehicle": truck['Vehicle'], "Hazard": "Heavy Rain", "Type": "Weather", "Message": f"🌧️ ระวังถนนลื่น! มีฝนตกหนักในพื้นที่"})
            if truck['PM 2.5'] > 100.0: alerts.append({"Vehicle": truck['Vehicle'], "Hazard": "Toxic Air", "Type": "Air Quality", "Message": f"😷 ฝุ่น PM2.5 หนาแน่นอันตราย"})
    return pd.DataFrame(alerts)



# ==========================================
# 🎨 4. UI / UX MAIN SYSTEM
# ==========================================
st.set_page_config(page_title="AuRORA | SaaS & API", page_icon="🌌", layout="wide")

if "logged_in" not in st.session_state: st.session_state.logged_in = False

# --- หน้าจอ LANDING PAGE & LOGIN ---
if not st.session_state.logged_in:
    st.markdown("""
    <div style="background: linear-gradient(135deg, #0f2027, #203a43, #2c5364); padding: 40px; border-radius: 15px; color: white; text-align: center; margin-bottom: 30px;">
        <h1 style="margin: 0; color: #00d2ff;">🌌 AuRORA OMNIVERSE SaaS</h1>
        <p style="margin-top: 15px; font-size: 20px; color: #a8c0ff;">The Ultimate AI Business Ecosystem & Fleet Command</p>
    </div>
    """, unsafe_allow_html=True)
    
    col_img, col_login = st.columns([1.2, 1])
    with col_img:
        st.markdown("### 🔥 สิทธิประโยชน์สำหรับผู้ใช้งาน")
        st.info("✅ **FREE TRIAL (30 วัน):** ใช้งานระบบ Fleet Tracking แจ้งเตือนเข้า Telegram ส่วนตัว\n\n💎 **VIP_USER_PRO:** ปลดล็อกระบบจัดการ API, ลิงก์เข้าศูนย์บัญชาการ VANGUARD")
    
    with col_login:
        tab1, tab2 = st.tabs(["🔑 เข้าสู่ระบบ", "📝 สมัครสมาชิก (ฟรี 30 วัน)"])
        with tab1:
            log_user = st.text_input("Username", key="log_user")
            log_pass = st.text_input("Password", type="password", key="log_pass")
            if st.button("เข้าสู่ระบบ", use_container_width=True, type="primary"):
                user_data = login_user(log_user, log_pass)
                if user_data:
                    st.session_state.logged_in = True
                    st.session_state.username = log_user
                    st.session_state.role = user_data[0]
                    # 🌟 รองรับทั้งข้อมูลแบบ String (ตอนสมัครใหม่) และ Date (ตอนดึงจาก PostgreSQL)
                    if isinstance(user_data[1], str):
                        st.session_state.expire_date = datetime.datetime.strptime(user_data[1], "%Y-%m-%d").date()
                    elif hasattr(user_data[1], 'date'):
                        st.session_state.expire_date = user_data[1].date()
                    else:
                        st.session_state.expire_date = user_data[1]
                    st.session_state.tier = user_data[2]
                    
                    # เก็บข้อมูล Telegram ของลูกค้าลง Session
                    st.session_state.tele_token = user_data[3] if user_data[3] else ""
                    st.session_state.tele_chat_id = user_data[4] if user_data[4] else ""
                    
                    send_telegram_alert(f"🟢 User <b>{log_user}</b> เข้าสู่ระบบสำเร็จ")
                    st.rerun()
                else: st.error("❌ Username หรือ Password ไม่ถูกต้อง")
                    
        with tab2:
            reg_user = st.text_input("ตั้ง Username ใหม่")
            reg_pass = st.text_input("ตั้ง Password ใหม่", type="password")
            if st.button("สมัครสมาชิกเลย", use_container_width=True):
                if register_user(reg_user, reg_pass):
                    st.success("✅ สมัครสมาชิกสำเร็จ! คุณได้รับสิทธิ์ใช้งานฟรี 30 วัน กรุณาเข้าสู่ระบบ")
                    send_telegram_alert(f"🎉 <b>มีผู้ใช้สมัครใหม่!</b>\nUser: {reg_user}\nTier: FREE_TRIAL")
                else: st.error("⚠️ Username นี้มีผู้ใช้งานแล้ว")

# --- หน้าจอ USER ---
elif st.session_state.role == "user":
    days_left = (st.session_state.expire_date - datetime.date.today()).days
    
    st.sidebar.markdown(f"👤 **{st.session_state.username}**")
    if st.session_state.tier == "VIP_USER_PRO": 
        st.sidebar.success("💎 **STATUS: VIP_USER_PRO**")
    else: 
        st.sidebar.info("⚪ **STATUS: FREE_TRIAL**")
    st.sidebar.markdown(f"⏳ **วันหมดอายุ:** {st.session_state.expire_date}")

    # 🌟 ตั้งค่า Telegram สำหรับลูกค้าระดับ FREE_TRIAL
    if st.session_state.tier != "VIP_USER_PRO":
        st.sidebar.markdown("---")
        st.sidebar.markdown("### 💬 ตั้งค่า Telegram ส่วนตัว")
        st.sidebar.caption("ใส่ข้อมูลบอทของคุณ เพื่อรับแจ้งเตือนสถานะรถ")
        
        new_token = st.sidebar.text_input("Bot Token", value=st.session_state.get('tele_token', ''), type="password")
        new_chat_id = st.sidebar.text_input("Chat ID", value=st.session_state.get('tele_chat_id', ''))
        
        if st.sidebar.button("💾 บันทึก Telegram", use_container_width=True):
            try:
                with engine.connect() as conn:
                    # 🛰️ อัปเดตข้อมูลลงฐานข้อมูลที่โตเกียว
                    conn.execute(text("""
                        UPDATE users 
                        SET tele_token = :t, tele_chat_id = :c 
                        WHERE username = :u
                    """), {
                        "t": new_token, 
                        "c": new_chat_id, 
                        "u": st.session_state.username
                    })
                    conn.commit() # ⚠️ สำคัญ: ต้อง Commit เพื่อยืนยันการบันทึกข้อมูลใน PostgreSQL
                
                # อัปเดตค่าในตัวแปรชั่วคราว (Session State) เพื่อให้ระบบใช้งานได้ทันทีไม่ต้อง Refresh
                st.session_state.tele_token = new_token
                st.session_state.tele_chat_id = new_chat_id
                st.sidebar.success("✅ อัปเดตข้อมูล Telegram สำเร็จ!")
                
            except Exception as e:
                st.sidebar.error(f"❌ เกิดข้อผิดพลาด: {e}")

    st.sidebar.markdown("---")
    if st.sidebar.button("🚪 ออกจากระบบ"):
        st.session_state.logged_in = False
        st.rerun()

    if days_left > 0:
        st.markdown(f"### ยินดีต้อนรับกลับมา, {st.session_state.username}!")
        
        if st.session_state.tier == "VIP_USER_PRO":
            with st.expander("🔑 ระบบจัดการ API (Developer Tools)", expanded=False):
                st.success("🎉 บัญชีระดับ VIP ของคุณพร้อมใช้งานแบบเต็มประสิทธิภาพแล้ว")
                
                with engine.connect() as conn:
                    # ดึงข้อมูล API Key จาก Supabase
                    query = text("SELECT api_key, created_at FROM api_keys WHERE username=:u AND status='ACTIVE'")
                    keys = conn.execute(query, {"u": st.session_state.username}).fetchall()
                
                if keys:
                    st.markdown("### Your API Keys:")
                    for k in keys: 
                        st.code(k[0], language="text")
                        st.caption(f"สร้างเมื่อ: {k[1]}")
                else:
                    st.info("คุณยังไม่มี API Key สำหรับเชื่อมต่อระบบ")
                    if st.button("➕ สร้าง API Key ใหม่", use_container_width=True): 
                        generate_api_key(st.session_state.username)
                        st.success("สร้าง Key สำเร็จ!")
                        st.rerun()

        else:
            st.warning(f"⏳ คุณใช้งานแบบ FREE TRIAL (เหลืออีก {days_left} วัน)")
            with st.expander("💎 กดเพื่ออัปเกรดเป็น VIP_USER_PRO (ปลดล็อก API ระบบองค์กร)"):
                st.markdown("#### สแกน PromptPay ชำระเงิน (AI ตรวจสลิปอัตโนมัติ)")
                
                PROMPTPAY_ID = "0845565562"  
                UPGRADE_PRICE = 50          
                
                
                col_qr, col_upload = st.columns(2)
                
                with col_qr: 
                    qr_url = f"https://promptpay.io/{PROMPTPAY_ID}/{UPGRADE_PRICE}.png"
                    st.image(qr_url, width=200, caption=f"สแกนเพื่อชำระเงิน {UPGRADE_PRICE} บาท")
                
                with col_upload:
                    uploaded_slip = st.file_uploader("📸 อัปโหลดสลิปโอนเงิน", type=["jpg", "png", "jpeg"])
                    
                    if uploaded_slip and st.button("🚀 ยืนยันและตรวจสอบสลิป (AI Verify)", type="primary"):
                        with st.spinner("🤖 AI Vision กำลังสแกนสลิปและตรวจสอบยอดเงิน..."):
                            
                            # 🚀 แปลงไฟล์ภาพทันที
                            slip_image = PIL.Image.open(io.BytesIO(uploaded_slip.getvalue()))
                            
                            try:
                                client = genai.Client(api_key=SLIP_CHECK_API_KEY)
                                prompt_check = f"คุณคือ AI ตรวจสอบสลิปโอนเงินธนาคาร หน้าที่ของคุณคือดูภาพนี้และเช็ค 3 ข้อ: 1. เป็นสลิปโอนเงินจริงหรือไม่ 2. ยอดเงินเท่ากับ {UPGRADE_PRICE} บาทหรือไม่ 3. สถานะโอนสำเร็จหรือไม่. หากผ่านทั้ง 3 ข้อให้ตอบว่า 'PASS' คำเดียว. หากไม่ผ่านให้ตอบ 'FAIL: [บอกเหตุผลสั้นๆ เช่น ยอดเงินไม่ตรง, ไม่ใช่สลิปโอนเงิน, หรือภาพเบลอ]'"
                                
                                res = client.models.generate_content(
                                    model='gemini-1.5-flash', 
                                    contents=[prompt_check, slip_image]
                                )
                                ai_result = res.text.strip()
                                
                                if "PASS" in ai_result.upper():
                                    # ✅ บันทึกลง Supabase
                                    with engine.connect() as conn:
                                        conn.execute(text("INSERT INTO payments (username, status) VALUES (:u, 'pending')"), 
                                                     {"u": st.session_state.username})
                                        conn.commit()
                                    
                                    send_telegram_slip(st.session_state.username, uploaded_slip.getvalue())
                                    st.success("✅ AI ตรวจสอบสลิปผ่าน! ยอดเงินถูกต้อง ระบบได้ส่งสลิปให้แอดมินอนุมัติแล้วครับ")
                                else:
                                    st.error(f"❌ AI ปฏิเสธสลิปของคุณ: {ai_result}")
                                    
                            except Exception as e:
                                st.warning("⚠️ เซิร์ฟเวอร์ AI ภาระงานสูง กำลังส่งต่อให้แอดมินตรวจสอบแมนนวล...")
                                # ✅ แผนสำรอง: บันทึกลง Supabase หาก AI ขัดข้อง
                                with engine.connect() as conn:
                                    conn.execute(text("INSERT INTO payments (username, status) VALUES (:u, 'pending')"), 
                                                 {"u": st.session_state.username})
                                    conn.commit()
                                    
                                send_telegram_slip(st.session_state.username, uploaded_slip.getvalue())
                                st.success("✅ ส่งสลิปให้แอดมินตรวจสอบเรียบร้อยแล้วครับ")

        # ==========================================
        # 🗺️ กำหนดทิศทางการส่ง Telegram อิงตามระดับผู้ใช้งาน
        # ==========================================
        if st.session_state.tier == "VIP_USER_PRO":
            # 🔐 ดึงค่ามาจาก Environment Variables ด้านบนสุด (ปลอดภัย 100%)
            ACTIVE_TELE_TOKEN = TELEGRAM_BOT_TOKEN
            ACTIVE_TELE_CHAT_ID = TELEGRAM_CHAT_ID
        else:
            ACTIVE_TELE_TOKEN = st.session_state.get('tele_token', '')
            ACTIVE_TELE_CHAT_ID = st.session_state.get('tele_chat_id', '')

        # ==========================================
        # 🗺️ นำหน้าจอ FLEET TRACKING (TAB 21) มาโชว์ให้ลูกค้าใช้งาน
        # ==========================================
        st.markdown("---")
        st.markdown("### 🧭 QUANTUM FLEET COMMAND (ระบบติดตามและคำนวณเบิกจ่าย)")
        
        st.markdown("#### 🌧️ เรดาร์ตรวจจับพายุและกลุ่มฝน (Live Weather Radar)")
        st.markdown("""
        <div style="border: 2px solid #00F0FF; border-radius: 12px; overflow: hidden; margin-bottom: 20px; box-shadow: 0 0 15px rgba(0, 240, 255, 0.2);">
            <iframe width="100%" height="350" src="https://embed.windy.com/embed.html?type=map&location=coordinates&metricRain=mm&metricTemp=°C&metricWind=km/h&zoom=6&overlay=rain&product=ecmwf&level=surface&lat=13.75&lon=100.50" frameborder="0"></iframe>
        </div>
        """, unsafe_allow_html=True)

        st.markdown("#### 📍 มาตรวัดการเดินทางและพฤติกรรมคนขับ")
        
       # 🚗 ฝังโค้ดแผนที่มิเตอร์วิ่งรถแบบ FULL OPTION (แผนที่สมบูรณ์ + Autocomplete + AI Filter + Multi-Stop)
        tracker_html = """
        <!DOCTYPE html>
        <html>
        <head>
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css" />
            <link rel="stylesheet" href="https://unpkg.com/leaflet-routing-machine@3.2.12/dist/leaflet-routing-machine.css" />
            <script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
            <script src="https://unpkg.com/leaflet-routing-machine@3.2.12/dist/leaflet-routing-machine.js"></script>
            
            <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tarekraafat/autocomplete.js@10.2.7/dist/css/autoComplete.02.min.css">
            <script src="https://cdn.jsdelivr.net/npm/@tarekraafat/autocomplete.js@10.2.7/dist/autoComplete.min.js"></script>
            
            <script src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js"></script>
            <script src="https://cdnjs.cloudflare.com/ajax/libs/html2pdf.js/0.10.1/html2pdf.bundle.min.js"></script>
            
            <style>
                @import url('https://fonts.googleapis.com/css2?family=Orbitron:wght@400;700&display=swap');
                body { background-color: #070B14; color: #FFFFFF; font-family: 'Orbitron', 'Sarabun', sans-serif; margin: 0; padding: 10px; }
                .setup-panel { background: #0B101E; border: 1px solid #9D00FF; padding: 20px; border-radius: 12px; display: flex; flex-wrap: wrap; gap: 15px; justify-content: center; margin-bottom: 20px; box-shadow: 0 0 20px rgba(157, 0, 255, 0.15); }
                .setup-item { display: flex; flex-direction: column; align-items: center; width: 140px; }
                .setup-input { background: #070B14; border: 1px solid #1E2D4A; color: #00F0FF; padding: 10px; border-radius: 8px; font-family: 'Sarabun', sans-serif; text-align: center; width: 100%; outline: none; transition: 0.3s; font-size: 13px; }
                
                .route-panel { background: rgba(0, 240, 255, 0.1); border: 1px solid #00F0FF; padding: 15px; border-radius: 12px; display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; margin-bottom: 20px; align-items: center;}
                
                /* 🔍 Custom Search Box Style */
                .autoComplete_wrapper { flex: 1; min-width: 250px; max-width: 400px; position: relative; }
                #dest-address { 
                    width: 100%; background: #070B14; border: 1px solid #00F0FF; color: #FFF; 
                    padding: 10px 15px; border-radius: 8px; font-family: 'Sarabun'; outline: none;
                    box-shadow: 0 0 10px rgba(0, 240, 255, 0.2); transition: 0.3s;
                }
                #dest-address:focus { border-color: #9D00FF; box-shadow: 0 0 15px rgba(157, 0, 255, 0.4); }
                
                /* 📋 Dropdown List Styling */
                .autoComplete_wrapper > ul { 
                    background-color: #0B101E !important; border: 1px solid #1E2D4A !important; 
                    border-radius: 8px !important; color: #FFF !important; padding: 5px 0 !important;
                    margin-top: 5px !important; box-shadow: 0 5px 20px rgba(0,0,0,0.5) !important;
                    max-height: 250px; overflow-y: auto; position: absolute; width: 100%; z-index: 1000;
                }
                .autoComplete_wrapper > ul > li { 
                    padding: 10px 15px !important; font-family: 'Sarabun'; font-size: 14px;
                    border-bottom: 1px solid rgba(255,255,255,0.05); cursor: pointer; transition: 0.2s;
                }
                .autoComplete_wrapper > ul > li:hover { background-color: #1E2D4A !important; color: #00F0FF !important; }
                .autoComplete_wrapper mark { background-color: transparent; color: #9D00FF; font-weight: bold; }

                .btn-search { background: linear-gradient(90deg, #9D00FF, #00F0FF); color: white; border: none; padding: 10px 20px; border-radius: 8px; cursor: pointer; font-weight: bold; font-family: 'Sarabun';}
                .btn-geofence { background: linear-gradient(90deg, #FF416C, #FF4B2B); color: white; border: none; padding: 10px 20px; border-radius: 8px; cursor: pointer; font-weight: bold; font-family: 'Sarabun';}
                
                .dashboard { display: grid; grid-template-columns: repeat(auto-fit, minmax(130px, 1fr)); gap: 15px; margin-bottom: 20px; }
                .meter-box { background: linear-gradient(145deg, #111A30, #0B101E); padding: 15px; border-radius: 12px; border: 1px solid #1E2D4A; text-align: center; display: flex; flex-direction: column; justify-content: center;}
                .meter-title { font-size: 11px; color: #8A99B5; letter-spacing: 1px; margin-bottom: 5px; }
                .meter-value { font-size: 24px; font-weight: bold; margin: 5px 0; text-shadow: 0 0 10px currentColor; }
                .ai-box { border: 1px solid #FF9500; background: rgba(255, 149, 0, 0.05); box-shadow: inset 0 0 15px rgba(255, 149, 0, 0.1); }
                
                .val-speed { color: #00F0FF; } .val-dist { color: #14F195; } .val-cost { color: #F3BA2F; } 
                .val-eta { color: #FF3B30; } .val-score { color: #14F195; } .val-traffic { color: #F3BA2F; font-size: 16px !important; } .val-rain { color: #00F0FF; font-size: 16px !important; }
                
                .controls-section { display: flex; flex-direction: column; align-items: center; gap: 15px; margin-bottom: 20px; }
                .btn-group { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; }
                button { font-family: 'Orbitron', 'Sarabun', sans-serif; font-weight: bold; font-size: 14px; padding: 12px 20px; border-radius: 8px; border: none; cursor: pointer; transition: 0.3s; }
                .btn-start { background: linear-gradient(90deg, #14F195, #0ea5e9); color: #000; }
                .btn-stop { background: linear-gradient(90deg, #FF3B30, #ff0055); color: #FFF; }
                .btn-reset { background: linear-gradient(90deg, #F3BA2F, #F9D423); color: #000; }
                .btn-export-pdf { background: linear-gradient(90deg, #FF416C, #FF4B2B); color: #FFF; }
                .btn-export-doc { background: linear-gradient(90deg, #0052D4, #4364F7); color: #FFF; }
                .btn-telegram { background: linear-gradient(90deg, #11998e, #38ef7d); color: #000; }
                
                .export-box { display: none; background: #0B101E; border: 1px solid #14F195; padding: 20px; border-radius: 12px; width: 100%; max-width: 600px; text-align: center; margin-top: 10px; }
                
                /* 🚨 บังคับความสูงและเฟรมของแผนที่ให้คงที่ ป้องกันบั๊กหายไป */
                #map-container { width: 100%; height: 480px; margin-bottom: 20px; border: 2px solid #00F0FF; border-radius: 12px; overflow: hidden; position: relative; }
                #map { width: 100%; height: 100%; }
                
                .log-container { background: #0B101E; border: 1px solid #1E2D4A; border-radius: 12px; padding: 15px; max-height: 200px; overflow-y: auto; font-size: 13px;}
                .log-time { color: #00F0FF; font-weight: bold; }
                .leaflet-routing-container { display: none !important; }
                
                #full-report-template { display: none; background: #FFFFFF; color: #000000; padding: 40px; font-family: 'Sarabun', sans-serif; font-size: 16px; border: 1px solid #ddd; }
                .report-table { width: 100%; border-collapse: collapse; margin-top: 20px; }
                .report-table th, .report-table td { border: 1px solid #333; padding: 10px; text-align: left; }
                .report-table th { background-color: #f2f2f2; width: 35%; }
            </style>
        </head>
        <body>
            <div class="setup-panel" id="setup-panel">
                <div class="setup-item"><span style="font-size:11px; color:#8A99B5;">🛰️ Fleet ID (รหัสรถ)</span><input type="text" id="fleet-id" class="setup-input" placeholder="เช่น CAR-01"></div>
                <div class="setup-item"><span style="font-size:11px; color:#8A99B5;">⛽ น้ำมัน (Km/L)</span><input type="number" id="fuel-kml" class="setup-input" value="12.5" step="0.1"></div>
                <div class="setup-item"><span style="font-size:11px; color:#8A99B5;">💰 ราคา (THB/L)</span><input type="number" id="fuel-price" class="setup-input" value="38.50" step="0.1"></div>
            </div>

            <div class="route-panel" id="route-panel" style="display:none;">
                <span style="font-size: 20px;">📍</span>
                <div class="autoComplete_wrapper" dir="ltr">
                    <input id="dest-address" type="search" dir="ltr" spellcheck=false autocorrect="off" autocomplete="off" capitalize="off" placeholder="ค้นหาสถานที่ (ใส่ลูกน้ำคั่นหลายจุดได้)">
                </div>
                <button class="btn-search" onclick="searchAndOptimizeRoute()">🤖 AI จัดเส้นทาง</button>
                <button class="btn-geofence" onclick="activateGeofenceMode()">🛡️ ตีรั้วดิจิทัล</button>
            </div>

            <div class="dashboard" id="ui-dashboard">
                <div class="meter-box"><div class="meter-title">SPEED (Km/h)</div><div class="meter-value val-speed" id="ui-speed">0.0</div></div>
                <div class="meter-box"><div class="meter-title">DISTANCE (Km)</div><div class="meter-value val-dist" id="ui-dist">0.00</div></div>
                <div class="meter-box"><div class="meter-title">ETA (เวลาถึง)</div><div class="meter-value val-eta" id="ui-eta">--:--</div></div>
                <div class="meter-box ai-box"><div class="meter-title" style="color:#FF9500;">🚦 TRAFFIC (จราจร)</div><div class="meter-value val-traffic" id="ui-traffic">รอนำทาง...</div></div>
                <div class="meter-box ai-box"><div class="meter-title" style="color:#00F0FF;">🌧️ RAIN RADAR (ฝน)</div><div class="meter-value val-rain" id="ui-rain-eta">รอสแกนเมฆฝน...</div></div>
                <div class="meter-box"><div class="meter-title">FUEL COST (THB)</div><div class="meter-value val-cost" id="ui-cost">0.00</div></div>
                <div class="meter-box"><div class="meter-title">SAFETY SCORE</div><div class="meter-value val-score" id="ui-score">100</div></div>
            </div>

            <div class="controls-section">
                <div class="btn-group">
                    <button class="btn-start" id="btn-start" onclick="startJourney()">▶ รับสัญญาณพิกัด GPS / เริ่มเดินทาง</button>
                    <button class="btn-stop" id="btn-stop" onclick="stopJourney()" style="display:none;">⏹ จบการเดินทาง / สรุปยอด</button>
                    <button class="btn-reset" id="btn-reset" onclick="resetJourney()" style="display:none;">🔄 เริ่มทริปใหม่ (Reset)</button>
                </div>

                <div class="export-box" id="export-box">
                    <h4 style="color: #14F195; margin-top: 0; margin-bottom: 15px;">📊 ดาวน์โหลดรายงานฉบับเต็ม</h4>
                    <div class="btn-group">
                        <button class="btn-telegram" id="btn-send-tele" onclick="sendSummaryToTelegram()">📲 ส่งรายงานเข้า Telegram</button>
                        <button class="btn-export-pdf" id="btn-export-pdf" onclick="exportPDF()">📥 โหลดเอกสาร PDF</button>
                        <button class="btn-export-doc" id="btn-export-doc" onclick="exportDoc()">📝 โหลดเอกสาร Word</button>
                    </div>
                </div>
            </div>

            <div id="map-container">
                <div id="map"></div>
            </div>

            <div class="log-container" id="log-list"><div style="color: #8A99B5;">รอรับสัญญาณดาวเทียมเพื่อตั้งต้น...</div></div>

            <div id="full-report-template">
                <div style="text-align: center; margin-bottom: 30px;">
                    <h2 style="color: #1E2D4A; margin-bottom: 5px;">AuRORA FLEET COMMAND</h2>
                    <h3 style="color: #666; margin-top: 0;">รายงานสรุปผลการเดินทางและสภาพแวดล้อม</h3>
                </div>
                <table class="report-table">
                    <tr><th>รหัสยานพาหนะ (Fleet ID)</th><td id="rep-fleet">-</td></tr>
                    <tr><th>จุดเริ่มต้น (Start Location)</th><td id="rep-start-loc">-</td></tr>
                    <tr><th>เส้นทางจัดส่ง (AI Optimized)</th><td id="rep-dest-loc">-</td></tr>
                    <tr><th>สภาพอากาศระหว่างทาง</th><td id="rep-weather">-</td></tr>
                    <tr><th>สภาพการจราจร (Traffic)</th><td id="rep-traffic">-</td></tr>
                    <tr><th>เวลาเริ่มเดินทาง (Start)</th><td id="rep-start-time">-</td></tr>
                    <tr><th>เวลาสิ้นสุด (End)</th><td id="rep-end-time">-</td></tr>
                    <tr><th>ระยะทางรวม (Distance)</th><td id="rep-dist">-</td></tr>
                    <tr><th>ความเร็วสูงสุด (Max Speed)</th><td id="rep-max-speed">-</td></tr>
                    <tr><th>พฤติกรรม (Safety Score)</th><td id="rep-score">-</td></tr>
                    <tr><th>ข้อมูลน้ำมัน (Fuel Setting)</th><td id="rep-kml">-</td></tr>
                    <tr><th>ค่าน้ำมันสุทธิ (Fuel Cost)</th><td id="rep-cost">-</td></tr>
                </table>
                <div style="margin-top: 50px; display: flex; justify-content: space-between;">
                    <div style="text-align: center;"><p>ลงชื่อ......................................................ผู้อนุมัติ</p></div>
                    <div style="text-align: center;"><p>ลงชื่อ......................................................ผู้ขับขี่</p></div>
                </div>
            </div>

            <script>
                let watchId = null; let map, polyline, marker; let pathCoordinates = [];
                let totalDistance = 0; let lastPosition = null; let fuelCost = 0; let prevSpeedKmh = 0;
                let safetyScore = 100; let routingControl = null;
                
                let geofenceCircle = null; let isInsideGeofence = false; let geofenceSetupMode = false;
                let maxSpeed = 0; let startLocName = "กำลังตรวจจับพิกัดต้นทาง..."; let destLocName = "ไม่มีเป้าหมายนำทาง (วิ่งอิสระ)";
                let weatherDesc = "ไม่มีข้อมูลพยากรณ์ฝน"; let trafficDesc = "ไม่มีข้อมูลจราจร";
                let startTimeStr = "-"; let endTimeStr = "-";

                var baseDark = L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', { attribution: '&copy; CARTO' });
                var trafficLayer = L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { opacity: 0.6 }); 
                var rainLayer = L.tileLayer('https://tile.openweathermap.org/map/precipitation_new/{z}/{x}/{y}.png?appid=137452d5d85d7b571eb339ea3debd099', { opacity: 0.7 });

                map = L.map('map', { center: [13.7563, 100.5018], zoom: 13, layers: [baseDark] });
                L.control.layers({ "โหมดมืด (Dark)": baseDark, "โหมดถนน (Street)": trafficLayer }, { "🌧️ เรดาร์กลุ่มฝน": rainLayer }).addTo(map);

                // 🌟 หน่วงเวลาโหลด Autocomplete ป้องกันบั๊กปุ่มกดไม่ติด
                let autoCompleteJS;
                function initAutoComplete() {
                    if(autoCompleteJS) return;
                    autoCompleteJS = new autoComplete({
                        selector: "#dest-address",
                        placeHolder: "พิมพ์ชื่อสถานที่, โรงแรม, ร้านอาหาร...",
                        data: {
                            src: async (query) => {
                                try {
                                    let parts = query.split(',');
                                    let searchTerm = parts[parts.length - 1].trim();
                                    if(searchTerm.length < 2) return [];
                                    
                                    const source = await fetch(`https://photon.komoot.io/api/?q=${encodeURIComponent(searchTerm)}&limit=5&lang=th`);
                                    const data = await source.json();
                                    return data.features;
                                } catch (error) { return []; }
                            },
                            keys: ["properties.name"]
                        },
                        resultsList: { maxResults: 5 },
                        resultItem: {
                            element: (item, data) => {
                                const props = data.value.properties;
                                const subtext = `${props.city || ''} ${props.state || ''} ${props.country || ''}`.trim();
                                item.innerHTML = `
                                    <div style="display:flex; flex-direction:column;">
                                        <span style="font-weight:bold;">${props.name}</span>
                                        <span style="font-size:11px; opacity:0.7;">${subtext}</span>
                                    </div>`;
                            },
                            highlight: true
                        },
                        events: {
                            input: {
                                selection: (event) => {
                                    const selection = event.detail.selection.value;
                                    let parts = document.querySelector("#dest-address").value.split(',');
                                    parts[parts.length - 1] = " " + selection.properties.name;
                                    document.querySelector("#dest-address").value = parts.join(',').trim();
                                }
                            }
                        }
                    });
                }

                function addLog(msg) {
                    const logList = document.getElementById('log-list');
                    const timeStr = new Date().toLocaleTimeString('th-TH');
                    logList.insertAdjacentHTML('afterbegin', `<div><span class="log-time">[${timeStr}]</span> ${msg}</div>`);
                }

                function sendTelegramLiveAlert(message) {
                    const token = "DYNAMIC_TELE_TOKEN"; 
                    const chatId = "DYNAMIC_TELE_CHAT_ID";
                    
                    if(!token || !chatId || token.trim() === "" || chatId.trim() === "") {
                        addLog("⚠️ [ระบบ]: คุณยังไม่ได้ตั้งค่า Telegram ในหน้าโปรไฟล์ การแจ้งเตือนจึงไม่ทำงาน");
                        return;
                    }

                    fetch(`https://api.telegram.org/bot${token}/sendMessage`, {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ chat_id: chatId, text: message, parse_mode: 'HTML' })
                    }).catch(e => console.log(e));
                }

                function sendSummaryToTelegram() {
                    const fleetId = document.getElementById('fleet-id').value || "UNKNOWN-CAR";
                    let msg = `🏁 <b>[AuRORA FLEET: JOURNEY COMPLETED]</b>\\n\\n`;
                    msg += `🚗 <b>รหัสรถ:</b> ${fleetId}\\n`;
                    msg += `📍 <b>เส้นทาง:</b> ${startLocName} ➡️ ${destLocName}\\n`;
                    msg += `🚦 <b>จราจร:</b> ${trafficDesc}\\n`;
                    msg += `🛣️ <b>วิ่งไป:</b> ${totalDistance.toFixed(2)} Km\\n`;
                    msg += `⚡ <b>ความเร็วสูงสุด:</b> ${maxSpeed.toFixed(1)} Km/h\\n`;
                    msg += `💵 <b>ค่าน้ำมัน:</b> ${fuelCost.toFixed(2)} บาท\\n`;
                    sendTelegramLiveAlert(msg);
                }

                function activateGeofenceMode() {
                    geofenceSetupMode = true;
                    addLog("🎯 [GEOFENCE] คลิกบนแผนที่เพื่อสร้างรั้วรัศมี 2 กิโลเมตร");
                    alert("คลิกบริเวณพื้นที่บนแผนที่ เพื่อสร้างรั้วควบคุม (Geofence) รัศมี 2 กิโลเมตร");
                }

                map.on('click', function(e) {
                    if(geofenceSetupMode) {
                        if(geofenceCircle) map.removeLayer(geofenceCircle);
                        geofenceCircle = L.circle(e.latlng, { color: '#FF4B2B', fillColor: '#FF4B2B', fillOpacity: 0.2, radius: 2000 }).addTo(map);
                        geofenceSetupMode = false;
                        isInsideGeofence = false; 
                        addLog("🛡️ สร้างรั้วดิจิทัลเรียบร้อย ระบบเริ่มเฝ้าระวัง...");
                    }
                });

                async function searchAndOptimizeRoute() {
                    let addrInput = document.getElementById('dest-address').value;
                    if(!addrInput) return;
                    if(!lastPosition) { alert("⚠️ รอสัญญาณ GPS ต้นทางก่อนครับ!"); return; }
                    
                    document.getElementById('ui-traffic').innerHTML = "AI กำลังคำนวณ...";
                    addLog(`🤖 [AI TSP] กำลังประมวลผลและเรียงลำดับจุดส่ง...`);
                    
                    let places = addrInput.split(',').map(p => p.trim()).filter(p => p.length > 0);
                    let geocodedPoints = [];

                    for(let place of places) {
                        try {
                            let res = await fetch('https://nominatim.openstreetmap.org/search?format=json&q=' + encodeURIComponent(place));
                            let data = await res.json();
                            if(data.length > 0) {
                                geocodedPoints.push({ name: place, lat: parseFloat(data[0].lat), lon: parseFloat(data[0].lon) });
                            }
                        } catch(e) { console.log(e); }
                        await new Promise(r => setTimeout(r, 500)); 
                    }

                    if(geocodedPoints.length === 0) { alert("❌ ไม่พบสถานที่ กรุณาลองใหม่"); return; }

                    let unvisited = [...geocodedPoints];
                    let currentPt = lastPosition;
                    let optimizedRoute = [];
                    
                    while(unvisited.length > 0) {
                        let closestIdx = 0; let minDist = Infinity;
                        for(let i=0; i<unvisited.length; i++) {
                            let d = getDistance(currentPt.lat, currentPt.lon, unvisited[i].lat, unvisited[i].lon);
                            if(d < minDist) { minDist = d; closestIdx = i; }
                        }
                        optimizedRoute.push(unvisited[closestIdx]);
                        currentPt = unvisited[closestIdx];
                        unvisited.splice(closestIdx, 1);
                    }

                    destLocName = optimizedRoute.map(p => p.name).join(" ➡️ ");
                    addLog(`🗺️ [AI Route] เส้นทางจัดเรียงแล้ว: ${destLocName}`);

                    let routeWaypoints = [ L.latLng(lastPosition.lat, lastPosition.lon) ];
                    optimizedRoute.forEach(pt => routeWaypoints.push(L.latLng(pt.lat, pt.lon)));

                    drawMultiRoute(routeWaypoints, destLocName);
                }

                function drawMultiRoute(waypoints, routeNames) {
                    if(routingControl) map.removeControl(routingControl);
                    routingControl = L.Routing.control({
                        waypoints: waypoints,
                        show: false, lineOptions: { styles: [{ color: '#00F0FF', opacity: 0.8, weight: 6 }] },
                        createMarker: function() { return null; }
                    }).on('routesfound', function(e) {
                        var summary = e.routes[0].summary;
                        var totalMins = Math.round(summary.totalTime / 60);
                        var dispTime = totalMins > 60 ? Math.floor(totalMins/60) + " ชม. " + (totalMins%60) + " น." : totalMins + " นาที";
                        document.getElementById('ui-eta').innerText = dispTime;
                        
                        let distKm = (summary.totalDistance / 1000).toFixed(2);
                        
                        let expectedTimeMins = distKm / 60 * 60; 
                        let trafficDelay = totalMins - expectedTimeMins;
                        let trafficStatus = "🟢 คล่องตัว"; let trafficColor = "#14F195";
                        if (trafficDelay > expectedTimeMins * 1.0) { trafficStatus = "🔴 ติดขัดสาหัส"; trafficColor = "#FF3B30"; }
                        else if (trafficDelay > expectedTimeMins * 0.3) { trafficStatus = "🟡 ชะลอตัว"; trafficColor = "#FF9500"; }
                        
                        trafficDesc = trafficStatus; 
                        document.getElementById('ui-traffic').innerHTML = `<span style="color:${trafficColor}">${trafficStatus}</span>`;

                        fetch(`https://api.open-meteo.com/v1/forecast?latitude=${waypoints[waypoints.length-1].lat}&longitude=${waypoints[waypoints.length-1].lng}&minutely_15=precipitation&current=wind_speed_10m`)
                        .then(res => res.json())
                        .then(wData => {
                            let precipList = wData.minutely_15.precipitation;
                            let rainEtaStr = "☀️ ท้องฟ้าโปร่ง"; let rainColor = "#14F195";
                            for(let i=0; i<12; i++) { 
                                if(precipList[i] > 0.1) {
                                    rainEtaStr = (i === 0) ? "⛈️ ฝนตกในพื้นที่!" : `🌧️ ฝนจะมาใน ${i*15} นาที`;
                                    rainColor = (i === 0) ? "#FF3B30" : "#FF9500"; break;
                                }
                            }
                            weatherDesc = rainEtaStr;
                            document.getElementById('ui-rain-eta').innerHTML = `<span style="color:${rainColor}">${rainEtaStr}</span>`;

                            const kmPerL = parseFloat(document.getElementById('fuel-kml').value) || 12.5;
                            const pricePerL = parseFloat(document.getElementById('fuel-price').value) || 38.5;
                            let estFuelCost = (parseFloat(distKm) / kmPerL) * pricePerL;

                            const fleetId = document.getElementById('fleet-id').value || "UNKNOWN-CAR";
                            let teleMsg = `📋 <b>[AuRORA AI: MULTI-STOP PLAN]</b>\\n\\n`;
                            teleMsg += `🚗 <b>รถ:</b> ${fleetId}\\n`;
                            teleMsg += `🗺️ <b>คิวส่งของ:</b> ${routeNames}\\n`;
                            teleMsg += `🚦 <b>การจราจร:</b> ${trafficStatus}\\n`;
                            teleMsg += `☁️ <b>สแกนเรดาร์:</b> ${weatherDesc}\\n\\n`;
                            teleMsg += `🛣️ <b>ระยะทางสุทธิ:</b> ${distKm} Km | ⏱️ <b>ETA:</b> ${dispTime}\\n`;
                            teleMsg += `💵 <b>ประเมินน้ำมัน:</b> ${estFuelCost.toFixed(2)} บาท\\n`; 
                            sendTelegramLiveAlert(teleMsg);

                        }).catch(e => { document.getElementById('ui-rain-eta').innerHTML = "ดาวเทียมขัดข้อง"; });
                    }).addTo(map);
                }

                function getDistance(lat1, lon1, lat2, lon2) {
                    var R = 6371; var dLat = (lat2 - lat1) * Math.PI / 180; var dLon = (lon2 - lon1) * Math.PI / 180; 
                    var a = Math.sin(dLat/2)*Math.sin(dLat/2) + Math.cos(lat1*Math.PI/180)*Math.cos(lat2*Math.PI/180)*Math.sin(dLon/2)*Math.sin(dLon/2); 
                    return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1-a)); 
                }

                function startJourney() {
                    if (!navigator.geolocation) { alert("GPS Not Supported"); return; }
                    document.getElementById('setup-panel').style.display = 'none';
                    document.getElementById('btn-start').style.display = 'none';
                    document.getElementById('btn-stop').style.display = 'inline-block';
                    document.getElementById('route-panel').style.display = 'flex';
                    
                    // 🌟 เลื่อนจังหวะเปิดระบบ AI ค้นหา มาไว้ตอนที่กล่องแสดงผลแล้ว! ป้องกันระบบพัง!
                    setTimeout(() => { initAutoComplete(); }, 100);
                    
                    const fleetId = document.getElementById('fleet-id').value || "UNKNOWN-CAR";
                    const kmPerL = parseFloat(document.getElementById('fuel-kml').value) || 12.5;
                    const pricePerL = parseFloat(document.getElementById('fuel-price').value) || 38.5;
                    
                    totalDistance = 0; safetyScore = 100; pathCoordinates = []; lastPosition = null; maxSpeed = 0;
                    if(polyline) map.removeLayer(polyline);
                    polyline = L.polyline([], {color: '#00F0FF', weight: 4}).addTo(map);

                    startTimeStr = new Date().toLocaleTimeString('th-TH');
                    addLog(`🚀 ระบบเดินเครื่อง พร้อมนำทาง...`);
                    sendTelegramLiveAlert(`🛰️ <b>[TRACKING STARTED]</b>\\n🚗 <b>รถ:</b> ${fleetId}\\n📍 สถานะ: ออกเดินทาง!`);

                    let isFirstFetch = true;

                    watchId = navigator.geolocation.watchPosition((position) => {
                        const lat = position.coords.latitude; const lon = position.coords.longitude;
                        let speedKmh = position.coords.speed ? position.coords.speed * 3.6 : 0;

                        if (lastPosition && speedKmh === 0) {
                            let dist = getDistance(lastPosition.lat, lastPosition.lon, lat, lon);
                            let timeDiff = (position.timestamp - lastPosition.timestamp) / 3600000;
                            // 🛡️ ป้องกัน GPS กระตุก: ถ้าเวลาผ่านไปน้อยกว่า 2 วินาที จะไม่นำมาคำนวณ
                            if(timeDiff > 0.0005) { 
                                speedKmh = dist / timeDiff;
                            }
                        }

                        // 🛡️ AI Noise Filter: ตัดพิกัดวาร์ป ถ้าความเร็วเกิน 200 km/h ให้ปัดทิ้ง
                        if (speedKmh > 200) {
                            speedKmh = prevSpeedKmh; 
                        }

                        if(isFirstFetch) {
                            isFirstFetch = false;
                            fetch(`https://nominatim.openstreetmap.org/reverse?format=json&lat=${lat}&lon=${lon}`)
                            .then(res => res.json())
                            .then(data => { startLocName = data.display_name; })
                        }

                        if (speedKmh > maxSpeed) maxSpeed = speedKmh;
                        document.getElementById('ui-speed').innerText = speedKmh.toFixed(1);

                        let speedDelta = speedKmh - prevSpeedKmh;
                        if (speedDelta > 20 || speedDelta < -20) { 
                            safetyScore = Math.max(0, safetyScore - 2);
                            document.getElementById('ui-score').innerText = safetyScore;
                        }
                        prevSpeedKmh = speedKmh;

                        if (lastPosition) {
                            totalDistance += getDistance(lastPosition.lat, lastPosition.lon, lat, lon);
                            document.getElementById('ui-dist').innerText = totalDistance.toFixed(2);
                            fuelCost = (totalDistance / kmPerL) * pricePerL;
                            document.getElementById('ui-cost').innerText = fuelCost.toFixed(2);
                        }

                        const latlng = [lat, lon];
                        pathCoordinates.push(latlng); polyline.setLatLngs(pathCoordinates); 
                        if(!routingControl) map.setView(latlng, 15);
                        if (!marker) marker = L.circleMarker(latlng, {color: '#14F195', radius: 8}).addTo(map); 
                        else marker.setLatLng(latlng);

                        if (geofenceCircle) {
                            let distToFence = getDistance(lat, lon, geofenceCircle.getLatLng().lat, geofenceCircle.getLatLng().lng) * 1000; 
                            let currentlyInside = distToFence <= geofenceCircle.getRadius();
                            
                            if (currentlyInside && !isInsideGeofence) {
                                isInsideGeofence = true;
                                sendTelegramLiveAlert(`⚠️ <b>[GEOFENCE ALERT]</b>\\n🚗 รถ: ${fleetId}\\n📥 เข้าสู่พื้นที่ควบคุมแล้ว!`);
                                addLog("⚠️ [GEOFENCE] รถเข้าสู่พื้นที่ควบคุมแล้ว!");
                            } else if (!currentlyInside && isInsideGeofence) {
                                isInsideGeofence = false;
                                sendTelegramLiveAlert(`⚠️ <b>[GEOFENCE ALERT]</b>\\n🚗 รถ: ${fleetId}\\n📤 ขับออกนอกพื้นที่ควบคุมแล้ว!`);
                                addLog("⚠️ [GEOFENCE] รถออกจากพื้นที่ควบคุมแล้ว!");
                            }
                        }

                        lastPosition = { lat: lat, lon: lon, timestamp: position.timestamp };
                    }, (err) => { alert("❌ อนุญาต GPS ก่อนครับ"); }, { enableHighAccuracy: true, maximumAge: 2000, timeout: 10000 });
                }

                function stopJourney() {
                    if (watchId) navigator.geolocation.clearWatch(watchId);
                    if (routingControl) map.removeControl(routingControl);
                    document.getElementById('btn-stop').style.display = 'none';
                    document.getElementById('route-panel').style.display = 'none';
                    document.getElementById('export-box').style.display = 'block';
                    document.getElementById('btn-reset').style.display = 'inline-block';
                    
                    document.getElementById('ui-speed').innerText = "0.0";
                    endTimeStr = new Date().toLocaleTimeString('th-TH');
                    sendSummaryToTelegram();
                }

                function resetJourney() {
                    document.getElementById('setup-panel').style.display = 'flex';
                    document.getElementById('btn-start').style.display = 'inline-block';
                    document.getElementById('export-box').style.display = 'none';
                    document.getElementById('btn-reset').style.display = 'none';
                    
                    document.getElementById('ui-speed').innerText = "0.0";
                    document.getElementById('ui-dist').innerText = "0.00";
                    document.getElementById('ui-cost').innerText = "0.00";
                    document.getElementById('ui-score').innerText = "100";
                    document.getElementById('ui-eta').innerText = "--:--";
                    document.getElementById('ui-traffic').innerHTML = "รอนำทาง...";
                    document.getElementById('ui-rain-eta').innerHTML = "รอสแกนเมฆฝน...";
                    
                    if (polyline) map.removeLayer(polyline);
                    if (marker) map.removeLayer(marker);
                    if (geofenceCircle) { map.removeLayer(geofenceCircle); geofenceCircle = null; }
                    
                    totalDistance = 0; safetyScore = 100; fuelCost = 0; maxSpeed = 0; isInsideGeofence = false;
                    startLocName = "รอพิกัด..."; destLocName = "วิ่งอิสระ";
                    pathCoordinates = [];
                    document.getElementById('log-list').innerHTML = '<div style="color: #8A99B5;">🔄 รีเซ็ตระบบ...</div>';
                }

                function populateReport() {
                    document.getElementById('rep-fleet').innerText = document.getElementById('fleet-id').value || "UNKNOWN";
                    document.getElementById('rep-start-loc').innerText = startLocName;
                    document.getElementById('rep-dest-loc').innerText = destLocName;
                    document.getElementById('rep-weather').innerText = weatherDesc;
                    document.getElementById('rep-traffic').innerText = trafficDesc;
                    document.getElementById('rep-start-time').innerText = startTimeStr;
                    document.getElementById('rep-end-time').innerText = endTimeStr;
                    document.getElementById('rep-dist').innerText = totalDistance.toFixed(2) + " Km";
                    document.getElementById('rep-max-speed').innerText = maxSpeed.toFixed(1) + " Km/h";
                    document.getElementById('rep-score').innerText = safetyScore + " / 100";
                    document.getElementById('rep-kml').innerText = `${document.getElementById('fuel-kml').value} Km/L`;
                    document.getElementById('rep-cost').innerText = fuelCost.toFixed(2) + " THB";
                }

                function exportPDF() {
                    populateReport();
                    const el = document.getElementById('full-report-template');
                    el.style.display = 'block'; 
                    html2pdf().from(el).set({ margin: 0.5, filename: 'AuRORA_Fleet_Report.pdf', html2canvas: { scale: 2 }, jsPDF: { unit: 'in', format: 'a4', orientation: 'portrait' } }).save().then(() => { el.style.display = 'none'; });
                }

                function exportDoc() {
                    populateReport();
                    let sourceHTML = "<html xmlns:o='urn:schemas-microsoft-com:office:office' xmlns:w='urn:schemas-microsoft-com:office:word' xmlns='http://www.w3.org/TR/REC-html40'><head><meta charset='utf-8'></head><body>" + document.getElementById('full-report-template').innerHTML + "</body></html>";
                    let fileDownload = document.createElement("a");
                    fileDownload.href = 'data:application/vnd.ms-word;charset=utf-8,' + encodeURIComponent(sourceHTML);
                    fileDownload.download = 'AuRORA_Fleet_Report.doc';
                    document.body.appendChild(fileDownload); fileDownload.click(); document.body.removeChild(fileDownload);
                }
            </script>
        </body>
        </html>
        """.replace("DYNAMIC_TELE_TOKEN", ACTIVE_TELE_TOKEN).replace("DYNAMIC_TELE_CHAT_ID", ACTIVE_TELE_CHAT_ID)
        
        components.html(tracker_html, height=1350)

    else:
        st.error("⛔ บัญชีของคุณหมดอายุการใช้งานแล้ว กรุณาอัปเกรดเพื่อใช้งานต่อ")

# --- หน้าจอ COMMANDER (ศูนย์บัญชาการสูงสุด VANGUARD) ---
elif st.session_state.role == "admin":
    st.sidebar.markdown(f"👑 **VANGUARD COMMANDER: {st.session_state.username}**")
    st.sidebar.success("🌐 STATUS: GLOBAL MONITOR ACTIVE")
    if st.sidebar.button("🚪 ออกจากระบบ", use_container_width=True): 
        st.session_state.logged_in = False
        st.rerun()
        
    st.title("🛰️ ศูนย์บัญชาการสูงสุด VANGUARD OMNIVERSE")

    tab_monitor, tab_manage = st.tabs(["🌍 แผนที่ติดตามเครือข่ายผู้ใช้งาน (Global Fleet)", "👥 จัดการบัญชีและอนุมัติสลิป"])

    with tab_monitor:
        st.markdown("### 🌐 เรดาร์ติดตามการเคลื่อนไหว AuRORA ทั้งระบบ")
        st.caption("มอนิเตอร์รถทุกคันที่กำลังออนไลน์ พร้อมดึงข้อมูลภัยพิบัติจาก NASA และสภาพอากาศแบบ Real-time")
        
        # เปิดหน้าจอนี้ทิ้งไว้ ระบบจะ Refresh แผนที่ให้เองทุกๆ 15 วินาที
        st_autorefresh(interval=15000, key="global_fleet_refresh")
        
        # ดึงข้อมูลผู้ใช้งานที่กำลังออนไลน์
        df_fleet = get_simulated_fleet() 
        df_hazards = fetch_nasa_hazards()
        df_alerts = calculate_risks(df_fleet, df_hazards)

        col1, col2, col3, col4 = st.columns(4)
        col1.metric("🟢 จำนวนรถที่ออนไลน์", f"{len(df_fleet)} คัน")
        col2.metric("🌪️ ภัยพิบัติ (NASA)", f"{len(df_hazards)} จุด")
        col3.metric("🚨 แจ้งเตือนความเสี่ยง", len(df_alerts), delta="ตรวจสอบ!" if len(df_alerts)>0 else "ปลอดภัย", delta_color="inverse")
        col4.metric("⏱️ อัปเดตล่าสุด", datetime.datetime.now().strftime("%H:%M:%S"))

        fig = go.Figure()
        
        # ชั้นข้อมูลที่ 1: พล็อตจุดอันตราย NASA
        if not df_hazards.empty:
            fig.add_trace(go.Scattermapbox(lat=df_hazards['Lat'], lon=df_hazards['Lon'], mode='markers', marker=go.scattermapbox.Marker(size=15, color='red', opacity=0.7), text=df_hazards['Title'], hoverinfo='text', name="⚠️ NASA Hazards"))

        # ชั้นข้อมูลที่ 2: พล็อตจุดรถยนต์ออนไลน์ทั้งหมด
        fig.add_trace(go.Scattermapbox(
            lat=df_fleet['Lat'], lon=df_fleet['Lon'], mode='markers+text', marker=go.scattermapbox.Marker(size=12, color='#00d2ff'),
            text=df_fleet['Vehicle'], textposition="bottom right",
            hovertext=[f"User: {v}<br>Speed: {s} km/h<br>Temp: {t}°C<br>PM2.5: {p}" for v, s, t, p in zip(df_fleet['Vehicle'], df_fleet['Speed (km/h)'], df_fleet['Temp (°C)'], df_fleet['PM 2.5'])], hoverinfo='text', name="🚚 Active Users"
        ))

        fig.update_layout(mapbox=dict(style="carto-darkmatter", zoom=5, center=dict(lat=13.5, lon=101.0)), margin={"r":0,"t":0,"l":0,"b":0}, height=550)
        st.plotly_chart(fig, use_container_width=True)

        st.markdown("#### 📋 ฐานข้อมูลพิกัดออนไลน์ (Raw Telemetry)")
        st.dataframe(df_fleet.style.background_gradient(cmap='Blues', subset=['Speed (km/h)']).format({"Lat": "{:.4f}", "Lon": "{:.4f}", "Temp (°C)": "{:.1f}°C", "Rain (mm)": "{:.1f}"}), use_container_width=True)

    with tab_manage:
        st.markdown("### 🔔 คำขออัปเกรดสถานะเป็น VIP_USER_PRO")
        
        # 🔗 เชื่อมต่อ Supabase ผ่าน SQLAlchemy Engine
        with engine.connect() as conn:
            # ดึงรายการที่รออนุมัติ (Pending) เรียงจากใหม่ไปเก่า
            query_pendings = text("SELECT id, username, timestamp FROM payments WHERE status='pending' ORDER BY timestamp DESC")
            pendings = conn.execute(query_pendings).fetchall()
            
            if not pendings:
                st.info("✅ ตอนนี้ไม่มีคำขอค้างอนุมัติครับ")
            else:
                for p in pendings:
                    payment_id, target_user, timestamp = p
                    
                    with st.expander(f"📥 แจ้งโอนจาก: {target_user} (เวลา: {timestamp})"):
                        st.write("💡 ตรวจสอบภาพสลิปได้ที่ห้อง Telegram VANGUARD (Log หลัก)")
                        
                        # กล่องรับเหตุผลกรณีต้องปฏิเสธ
                        reject_reason = st.text_input(
                            "ระบุเหตุผลหากต้องการปฏิเสธ (เช่น สลิปเบลอ, ยอดไม่ตรง)", 
                            key=f"reason_{payment_id}"
                        )
                        
                        col1, col2 = st.columns(2)
                        
                        # --- ปุ่มอนุมัติ (Approve) ---
                        if col1.button(f"✅ อนุมัติ VIP", key=f"app_{payment_id}", use_container_width=True):
                            # 1. คำนวณวันหมดอายุใหม่ (บวกเพิ่ม 30 วันจากวันปัจจุบันหรือวันหมดอายุเดิม)
                            user_info = conn.execute(text("SELECT expire_date FROM users WHERE username=:u"), {"u": target_user}).fetchone()
                            
                            current_expire = user_info[0] if user_info else datetime.date.today()
                            new_expire = max(datetime.date.today(), current_expire) + datetime.timedelta(days=30)
                            
                            # 2. อัปเดตสถานะ User และ Payment ใน Supabase
                            conn.execute(text("UPDATE users SET expire_date=:e, tier='VIP_USER_PRO' WHERE username=:u"), {"e": new_expire, "u": target_user})
                            conn.execute(text("UPDATE payments SET status='approved' WHERE id=:id"), {"id": payment_id})
                            conn.commit()
                            
                            # 3. ส่ง Telegram แจ้งข่าวดีให้ลูกค้า (ถ้าเขาตั้งค่าบอทส่วนตัวไว้)
                            user_tele = conn.execute(text("SELECT tele_token, tele_chat_id FROM users WHERE username=:u"), {"u": target_user}).fetchone()
                            if user_tele and user_tele[0]:
                                try:
                                    msg = "🎉 <b>ยินดีด้วย!</b>\nบัญชีของคุณได้รับการอัปเกรดเป็น <b>VIP_USER_PRO</b> เรียบร้อยแล้วครับ!"
                                    requests.post(f"https://api.telegram.org/bot{user_tele[0]}/sendMessage", 
                                                  json={"chat_id": user_tele[1], "text": msg, "parse_mode": "HTML"})
                                except: pass
                            
                            st.success(f"🌟 อนุมัติสิทธิ์ VIP ให้คุณ {target_user} เรียบร้อย!")
                            st.rerun()

                        # --- ปุ่มปฏิเสธ (Reject) ---
                        if col2.button(f"❌ ปฏิเสธ", key=f"rej_{payment_id}", use_container_width=True):
                            # 1. อัปเดตสถานะเป็น Rejected
                            conn.execute(text("UPDATE payments SET status='rejected' WHERE id=:id"), {"id": payment_id})
                            conn.commit()
                            
                            reason_text = reject_reason if reject_reason else "สลิปไม่ถูกต้อง/ข้อมูลไม่ครบถ้วน"
                            
                            # 2. ส่ง Telegram แจ้งเหตุผลให้ลูกค้าทราบ
                            user_tele = conn.execute(text("SELECT tele_token, tele_chat_id FROM users WHERE username=:u"), {"u": target_user}).fetchone()
                            if user_tele and user_tele[0]:
                                try:
                                    msg = f"⚠️ <b>การชำระเงินถูกปฏิเสธ</b>\n💬 เหตุผล: {reason_text}\nกรุณาตรวจสอบและอัปโหลดสลิปใหม่อีกครั้งครับ"
                                    requests.post(f"https://api.telegram.org/bot{user_tele[0]}/sendMessage", 
                                                  json={"chat_id": user_tele[1], "text": msg, "parse_mode": "HTML"})
                                except: pass
                            
                            st.error(f"ปฏิเสธรายการของ {target_user} แล้ว")
                            st.rerun()
