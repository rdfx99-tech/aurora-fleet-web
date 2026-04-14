import os
import time
import requests
import datetime
import schedule
from sqlalchemy import create_engine, text # 🌟 [เพิ่มใหม่] นำเข้าเครื่องมือเชื่อมต่อฐานข้อมูล

# ==========================================
# 🔑 การตั้งค่าศูนย์บัญชาการ (Config)
# ==========================================
VIP_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  
VIP_CHAT = os.getenv("TELEGRAM_CHAT_ID")
NASA_API_KEY = os.getenv("NASA_API_KEY", "DEMO_KEY")
DB_URL = os.getenv("DATABASE_URL") # 🌟 [เพิ่มใหม่] รับกุญแจโกดัง Supabase

# 🌟 [เพิ่มใหม่] สร้างท่อเชื่อมต่อฐานข้อมูล
if DB_URL:
    engine = create_engine(DB_URL)
else:
    engine = None
    print("⚠️ ระบบแจ้งเตือน: ไม่พบ DATABASE_URL ใน Environment")

def send_telegram_alert(message):
    """ฟังก์ชันยิงแจ้งเตือนเข้าห้อง VIP (ของเดิม)"""
    if not VIP_TOKEN or not VIP_CHAT: return
    url = f"https://api.telegram.org/bot{VIP_TOKEN}/sendMessage"
    payload = {"chat_id": VIP_CHAT, "text": message, "parse_mode": "HTML"}
    try:
        requests.post(url, json=payload, timeout=10)
        print(f"[{datetime.datetime.now()}] 📡 ยิงสัญญาณเตือนภัยเข้า Telegram สำเร็จ!")
    except Exception as e:
        print(f"❌ ส่งข้อความล้มเหลว: {e}")

# ==========================================
# 💾 [เพิ่มใหม่] ระบบผู้สื่อข่าว (ยิงข่าวด่วนเข้า Supabase)
# ==========================================
def save_news_to_supabase(news_text, severity="warning", lat=0.0, lon=0.0, is_vip=False):
    """ฟังก์ชันบันทึกข่าวด่วนลงฐานข้อมูล เพื่อให้หน้าเว็บดึงไปทำ 'ตัวหนังสือวิ่ง'"""
    if not engine: return
    
    try:
        with engine.connect() as conn:
            # 1. สร้างตาราง live_news ให้อัตโนมัติ (ถ้ายังไม่มี)
            conn.execute(text('''
                CREATE TABLE IF NOT EXISTS live_news (
                    id SERIAL PRIMARY KEY,
                    news_text TEXT,
                    severity TEXT,
                    lat FLOAT,
                    lon FLOAT,
                    is_vip_only BOOLEAN,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            '''))
            
            # 2. นำข่าวด่วนใส่ลงไปในโกดัง
            conn.execute(text("""
                INSERT INTO live_news (news_text, severity, lat, lon, is_vip_only)
                VALUES (:text, :sev, :lat, :lon, :vip)
            """), {
                "text": news_text,
                "sev": severity,
                "lat": lat,
                "lon": lon,
                "vip": is_vip
            })
            conn.commit()
            print(f"[{datetime.datetime.now()}] 💾 บันทึกข่าวด่วนลง Supabase สำเร็จ! ({severity.upper()})")
    except Exception as e:
        print(f"❌ ไม่สามารถบันทึกข่าวลงฐานข้อมูลได้: {e}")

# ==========================================
# 🧠 หุ่นยนต์วิเคราะห์ภัยพิบัติ (Disaster Parsers)
# ==========================================
def check_earthquakes():
    """ตรวจสอบแผ่นดินไหว 5.5+ จาก USGS"""
    try:
        res = requests.get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/5.5_day.geojson", timeout=10)
        eq_data = res.json().get("features", [])
        
        for eq in eq_data:
            mag = float(eq['properties']['mag'])
            place = eq['properties']['place']
            coords = eq['geometry']['coordinates'] # [lon, lat, depth]
            
            # 1. ยิงแจ้งเตือน Telegram
            msg = f"⚠️ <b>[EARTHQUAKE ALERT]</b> ⚠️\n\n🌍 <b>ขนาด:</b> {mag} ริกเตอร์\n📍 <b>จุดเกิดเหตุ:</b> {place}\n\n🚨 <i>โปรดเฝ้าระวังอาฟเตอร์ช็อกและสึนามิในพื้นที่ใกล้เคียง</i>"
            send_telegram_alert(msg)
            
            # 2. 🌟 ยิงข่าวด่วนขึ้นหน้าเว็บ OMNIVERSE
            # ถ้าเกิน 6.5 ถือว่าวิกฤต (Critical) สีแดงแจ้งเตือนหน้าจอ
            news_severity = "critical" if mag >= 6.5 else "warning"
            save_news_to_supabase(
                news_text=f"USGS ด่วน: แผ่นดินไหวรุนแรง {mag} ริกเตอร์ บริเวณ {place}",
                severity=news_severity,
                lat=coords[1],
                lon=coords[0],
                is_vip=False
            )
    except: pass

def check_geomagnetic_storms():
    """ตรวจสอบพายุสุริยะ (กระทบ GPS/ดาวเทียม)"""
    try:
        res = requests.get("https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json", timeout=10)
        kp_index = float(res.json()[-1][1])
        
        if kp_index >= 6:
            # 1. ยิงแจ้งเตือน Telegram
            msg = f"🌌 <b>[GEOMAGNETIC STORM WARNING]</b> 🌌\n\n🌩️ <b>ดัชนีพายุแม่เหล็กโลก:</b> {kp_index}\n⚠️ <i>อาจเกิดความปั่นป่วนของสัญญาณ GPS, ดาวเทียมสื่อสาร</i>"
            send_telegram_alert(msg)
            
            # 2. 🌟 ยิงข่าวด่วนขึ้นหน้าเว็บ OMNIVERSE (ข่าวอวกาศให้ VIP เห็นก่อนเป็นสีทอง)
            save_news_to_supabase(
                news_text=f"พายุสุริยะระดับรุนแรง (Kp {kp_index}) เข้าปะทะโลก อาจรบกวนสัญญาณดาวเทียมและระบบเทรด",
                severity="critical",
                lat=0.0, lon=0.0, # ภัยระดับโลก ไม่มีพิกัดเจาะจง
                is_vip=True # 💎 เซ็ตเป็นข่าว VIP
            )
    except: pass

def run_all_checks():
    """รวมศูนย์สั่งการ OMNI-SENTINEL"""
    print(f"[{datetime.datetime.now()}] 🤖 OMNI-SENTINEL ตื่นขึ้นมาสแกนโลก...")
    check_earthquakes()
    check_geomagnetic_storms()
    print("✅ สแกนเสร็จสิ้น กลับเข้าสู่โหมดสลีป")

# ==========================================
# ⏰ ระบบสั่งรัน 1 รอบ (สำหรับ GitHub Actions)
# ==========================================
if __name__ == "__main__":
    print("========================================")
    print(" 🛰️ AuRORA SENTINEL BOT: GITHUB ACTIONS ONLINE ")
    print("========================================")
    
    run_all_checks() # รันแค่รอบเดียวแล้วจบการทำงานทันที
