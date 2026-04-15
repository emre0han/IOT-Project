import time
import board
import busio
import RPi.GPIO as GPIO
import Adafruit_DHT
from ina219 import INA219
import adafruit_mpu6050
from RPLCD.i2c import CharLCD
import joblib
import pandas as pd
import csv
import os
from datetime import datetime
import warnings
import requests 

# Gereksiz uyarıları gizle
warnings.filterwarnings("ignore")

# ----------------- AYARLAR -----------------
# !!! BURAYI DOLDURMAYI UNUTMA !!!
import os
THINGSPEAK_API_KEY = os.getenv("THINGSPEAK_API_KEY")
THINGSPEAK_URL = "https://api.thingspeak.com/update"

GPIO.setmode(GPIO.BCM)
GPIO.setwarnings(False)

# PINLER
SERVO_PIN  = 18        
HALL_PIN   = 22        
GAS_PIN    = 23            
BUZZER_PIN = 24        
DHT_PIN    = 8              

# --- GÜVENLİK LİMİTLERİ (BU SINIRLAR AŞILIRSA MOTOR DURUR) ---
SICAKLIK_LIMIT = 28   # 28°C üstü -> MOTOR DURUR
NEM_LIMIT      = 70   # %70 nem üstü -> MOTOR DURUR
RPM_LIMIT      = 150  # 150 RPM üstü -> MOTOR DURUR
ACI_ALT_LIMIT  = 80   # 80° altı -> MOTOR DURUR
ACI_UST_LIMIT  = 100  # 100° üstü -> MOTOR DURUR
AKIM_LIMIT_mA  = 3000 # 3000mA (1.5A) üstü -> MOTOR DURUR

DHT_SENSOR = Adafruit_DHT.DHT11
VERITABANI = 'veritabani.csv'
MODEL_DOSYASI = 'turbin_beyni.pkl'

# ----------------- KURULUM -----------------
GPIO.setup(SERVO_PIN, GPIO.OUT)
GPIO.setup(HALL_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(GAS_PIN, GPIO.IN)
GPIO.setup(BUZZER_PIN, GPIO.OUT)

pwm = GPIO.PWM(SERVO_PIN, 50)
pwm.start(0)
GPIO.output(BUZZER_PIN, GPIO.LOW) 

i2c = busio.I2C(board.SCL, board.SDA)
ina = None; mpu = None; lcd = None

# --- Sensör Başlatma Blokları ---
print("-" * 50)
print("SİSTEM BAŞLATILIYOR...")
print("-" * 50)

try: 
    ina = INA219(0.1, 2.0, address=0x40)
    ina.configure()
    print("✅ Güç Sensörü (INA219) Bağlandı")
except: 
    print("⚠ Güç Sensörü YOK - Akım koruması devre dışı.")

try: 
    mpu = adafruit_mpu6050.MPU6050(i2c, address=0x68)
    print("✅ Açı Sensörü (MPU6050) Bağlandı")
except: 
    print("⚠ Açı Sensörü YOK")

try: 
    lcd = CharLCD('PCF8574', 0x27, port=1, charmap='A00', cols=20, rows=4)
    lcd.clear()
    lcd.write_string("Sistem Aktif")
    print("✅ LCD Ekran Bağlandı")
except: 
    print("⚠ LCD Ekran YOK")

print("Yapay Zeka Yükleniyor...")
try:
    model = joblib.load(MODEL_DOSYASI)
    print("✅ Yapay Zeka (Beyin) Hazır")
except:
    print("❌ HATA: Beyin dosyası bulunamadı! Varsayılan modda çalışacak.")
    model = None

# CSV Dosyası Başlıkları
if not os.path.exists(VERITABANI):
    with open(VERITABANI, mode='w', newline='') as f:
        csv.writer(f).writerow(['Tarih', 'Saat', 'Sicaklik', 'Nem', 'RPM', 'Aci', 'Gaz', 'Voltaj', 'Akim', 'Guc', 'Durum'])

# ----------------- ANA DÖNGÜ DEĞİŞKENLERİ -----------------
pulse_count = 0
last_rpm_time = 0
hall_prev = 1
last_ai_time = 0
last_thingspeak_time = 0 

# Başlangıç Değerleri (Sensör hatası olursa bu değerler veya son okunan değerler kullanılır)
sicaklik = 25.0
nem = 50.0
rpm = 0.0
aci = 90
voltaj = 0.0
akim = 0.0
guc = 0.0
gaz_val_ai = 0

pwm.ChangeDutyCycle(10) # Başlangıç hızı

print("\n" + "=" * 60)
print("GÜVENLİK PROTOKOLÜ AKTİF: AŞAĞIDAKİ DURUMLARDA MOTOR DURUR:")
print(f"1. Gaz Kaçağı Varsa")
# Sensör bağlantısı kopsa bile sistem artık durmayacak (Son veriyi kullanır)
print(f"2. Sıcaklık > {SICAKLIK_LIMIT}°C")
print(f"3. Nem > %{NEM_LIMIT}")
print(f"4. Hız > {RPM_LIMIT} RPM")
print(f"5. Açı < {ACI_ALT_LIMIT}° veya > {ACI_UST_LIMIT}°")
print(f"6. Akım > {AKIM_LIMIT_mA} mA")
print("=" * 60 + "\n")

try:
    while True:
        now = time.time()
        
        # --- RPM Sayacı (Kesintisiz) ---
        hall_curr = GPIO.input(HALL_PIN)
        if hall_prev == 1 and hall_curr == 0:
            pulse_count += 1
        hall_prev = hall_curr

        # --- 2 Saniyede Bir Kontrol ve Karar ---
        if now - last_ai_time > 2.0:
            # 1. RPM Hesapla
            rpm = (pulse_count / (now - last_rpm_time)) * 60
            pulse_count = 0
            last_rpm_time = now
            
            # ----------------------------------------------------------------
            # TÜM SENSÖRLERİ OKU
            # ----------------------------------------------------------------
            
            # A. Sıcaklık/Nem Oku
            okunan_nem, okunan_sicaklik = Adafruit_DHT.read(DHT_SENSOR, DHT_PIN)
            if okunan_nem is not None and okunan_sicaklik is not None:
                sicaklik = okunan_sicaklik
                nem = okunan_nem
            else:
                # EĞER SENSÖR HATALIYSA:
                # Eski "None" yapma kısmını kaldırdık.
                # Değişkenler bir önceki döngüdeki değerlerini (veya başlangıç değerini) korur.
                pass 

            # B. Gaz Oku
            if GPIO.input(GAS_PIN) == 0:
                gaz_val_ai = 1
                gaz_str = "VAR!"
            else:
                gaz_val_ai = 0
                gaz_str = "YOK"

            # C. Açı Oku
            if mpu:
                try:
                    raw_aci = int((mpu.acceleration[0] + 10) * 9)
                    aci = max(0, min(180, raw_aci))
                except: pass
            
            # D. Güç Oku
            if ina:
                try:
                    voltaj = ina.voltage()
                    akim = abs(ina.current())
                    guc = (voltaj * akim) / 1000.0
                except: pass

            # ----------------------------------------------------------------
            # MERKEZİ GÜVENLİK KONTROLÜ - TÜM SENSÖRLER
            # ----------------------------------------------------------------
            motor_durdurma_sebebi = None # Boşsa her şey yolunda

            # NOT: Kablo kopukluk/Sensör hatası kontrolü buradan kaldırılmıştır.
            
            # 1. GAZ KONTROLÜ
            if gaz_val_ai == 1:
                motor_durdurma_sebebi = "GAZ KACAGI"

            # 2. SICAKLIK LİMİT KONTROLÜ
            elif sicaklik > SICAKLIK_LIMIT:
                motor_durdurma_sebebi = "YUKSEK SICAKLIK"

            # 3. NEM LİMİT KONTROLÜ
            elif nem > NEM_LIMIT:
                motor_durdurma_sebebi = "YUKSEK NEM"

            # 4. RPM (HIZ) KONTROLÜ
            elif rpm > RPM_LIMIT:
                motor_durdurma_sebebi = "YUKSEK HIZ"

            # 5. AÇI (DENGE) KONTROLÜ
            elif (aci < ACI_ALT_LIMIT or aci > ACI_UST_LIMIT):
                motor_durdurma_sebebi = "ACI HATASI"

            # 6. AKIM (ZORLANMA) KONTROLÜ
            elif akim > AKIM_LIMIT_mA:
                motor_durdurma_sebebi = "ASIRI AKIM"


            # ----------------------------------------------------------------
            # AKSİYON AL (MOTORU DURDUR VEYA DEVAM ET)
            # ----------------------------------------------------------------
            
            if motor_durdurma_sebebi:
                # !!! TEHLİKE !!! -> MOTORU KES
                pwm.ChangeDutyCycle(0) 
                GPIO.output(BUZZER_PIN, GPIO.HIGH) # Alarm çal
                
                print(f"🛑 ACİL DURUM: {motor_durdurma_sebebi} TESPİT EDİLDİ! -> MOTOR KAPATILDI.")
                durum_mesaji = motor_durdurma_sebebi
                
                if lcd:
                    lcd.clear()
                    lcd.write_string(f"! {motor_durdurma_sebebi} !")
                    lcd.cursor_pos = (1,0)
                    lcd.write_string("MOTOR DURDURULDU")
                
            else:
                # HER ŞEY NORMAL -> ÇALIŞMAYA DEVAM
                GPIO.output(BUZZER_PIN, GPIO.LOW)
                
                # AI Tahmin (Sistem sağlıklıyken çalışır)
                durum_mesaji = "NORMAL"
                if model:
                    try:
                        veri = pd.DataFrame([[sicaklik, nem, rpm, aci, gaz_val_ai, voltaj, akim, guc]], 
                                            columns=['Sicaklik', 'Nem', 'RPM', 'Aci', 'Gaz', 'Voltaj', 'Akim', 'Guc'])
                        tahmin = model.predict(veri)
                        if tahmin[0] == -1: durum_mesaji = "ANOMALI (AI)"
                    except: pass
                
                # Normal Hız
                pwm.ChangeDutyCycle(10)
                
                print(f"Isı:{sicaklik:.0f}°C | Nem:%{nem:.0f} | RPM:{int(rpm)} | Açı:{aci} | Gaz:{gaz_str} | Akım:{akim:.0f}mA | {durum_mesaji}")

                if lcd:
                    lcd.clear()
                    lcd.write_string(f"T:{sicaklik:.0f} R:{int(rpm)} A:{aci}")
                    lcd.cursor_pos = (1, 0)
                    lcd.write_string("SISTEM NORMAL")

            # ----------------------------------------------------------------
            # KAYIT (CSV)
            # ----------------------------------------------------------------
            with open(VERITABANI, mode='a', newline='') as f:
                zaman_str = datetime.now().strftime("%H:%M:%S")
                tarih_str = datetime.now().strftime("%Y-%m-%d")
                csv.writer(f).writerow([tarih_str, zaman_str, 
                                     sicaklik, nem, int(rpm), aci, gaz_val_ai, voltaj, akim, guc, durum_mesaji])

            # ----------------------------------------------------------------
            # THINGSPEAK'E GÖNDERME
            # ----------------------------------------------------------------
            if now - last_thingspeak_time > 16.0: 
                try:
                    payload = {
                        "api_key": THINGSPEAK_API_KEY,
                        "field1": sicaklik,
                        "field2": nem,
                        "field3": aci,
                        "field4": akim,
                        "field5": int(rpm),
                        "field6": gaz_val_ai
                    }
                    requests.get(THINGSPEAK_URL, params=payload, timeout=2)
                    print("☁️ Veriler ThingSpeak'e gönderildi.")
                    last_thingspeak_time = now 
                except Exception as e:
                    print(f"⚠️ Bulut Yükleme Hatası: ({e})")

            last_ai_time = now
        
        time.sleep(0.002)

except KeyboardInterrupt:
    print("\nProgram Kapatılıyor...")
    GPIO.cleanup()
    pwm.stop()
    if lcd: lcd.clear()
    print("✅ Güvenli çıkış yapıldı.")
