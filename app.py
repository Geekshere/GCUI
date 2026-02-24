import os
import subprocess
import shutil
import datetime
import re
import smtplib
import threading
import time
import json
from email.mime.text import MIMEText
from flask import Flask, render_template, jsonify, request, send_from_directory, send_file
from PIL import Image

# Disable DOS attack protection for massive satellite images
Image.MAX_IMAGE_PIXELS = None

app = Flask(__name__)

# --- CONFIG ---
BASE_DIR = os.path.expanduser("~/SatDump/build/elektro_l3_output")
CAPTURE_DIR = os.path.join(BASE_DIR, "IMAGE/")
CLOUD_VIEW_DIR = os.path.expanduser("~/iCloud_View")
THUMB_DIR = os.path.expanduser("~/mission_control/thumbs")
CACHE_FILE = os.path.expanduser("~/mission_control/files_cache.json")

TUNNEL_LOG = "/home/ethan/mission_control/tunnel.log"
LAST_LINK_FILE = "/home/ethan/mission_control/last_link.txt"
RECIPIENT_FILE = "/home/ethan/mission_control/recipient.txt"
ADMIN_PIN = "9494"
ICLOUD_USER = "ethangotz@icloud.com"
ICLOUD_PASS = "ctaf-ktli-ilth-ewvg"

os.makedirs(CAPTURE_DIR, exist_ok=True)
os.makedirs(CLOUD_VIEW_DIR, exist_ok=True)
os.makedirs(THUMB_DIR, exist_ok=True)

thumb_lock = threading.Lock()

# --- THE 16-BIT TRANSLATOR ---
def make_jpeg_safe(img):
    # Convert 16-bit scientific grayscale to 8-bit with Auto-Exposure (Normalization)
    if img.mode in ('I', 'I;16', 'I;16B', 'I;16L', 'F'):
        # Find the actual range of the image data
        low, high = img.getextrema()
        # Prevent division by zero if image is solid
        if high <= low: high = low + 1
        # Stretch the real data to fit 0-255 perfectly
        img = img.point(lambda i: (i - low) * (255.0 / (high - low))).convert('L')
    
    # Handle transparent backgrounds (turn them black for space images)
    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
        bg = Image.new("RGB", img.size, (0, 0, 0))
        bg.paste(img, mask=img.convert('RGBA').split()[-1])
        return bg
        
    # Convert everything else to standard RGB
    if img.mode != 'RGB':
        return img.convert('RGB')
    return img

# --- THE PROACTIVE BACKGROUND FACTORY ---
def background_processor():
    while True:
        try:
            if os.path.exists(CLOUD_VIEW_DIR):
                current_files = []
                for root, dirs, filenames in os.walk(CLOUD_VIEW_DIR):
                    for f in filenames:
                        if f.lower().endswith(('.png', '.jpg', '.jpeg')):
                            full_path = os.path.join(root, f)
                            rel_path = os.path.relpath(full_path, CLOUD_VIEW_DIR)
                            safe_name = rel_path.replace("/", "_")
                            thumb_path = os.path.join(THUMB_DIR, safe_name)
                            
                            # 1. Pre-Generate Thumbnail if missing
                            if not os.path.exists(thumb_path):
                                try:
                                    with Image.open(full_path) as img:
                                        img = make_jpeg_safe(img) # MUST BE FIRST!
                                        img.thumbnail((1200, 1200)) 
                                        img.save(thumb_path, "JPEG", quality=85)
                                except Exception:
                                    pass 
                            
                            # 2. Add to Instant Cache List
                            try:
                                stat = os.stat(full_path)
                                current_files.append({
                                    "name": rel_path,
                                    "ts": stat.st_mtime,
                                    "size_mb": round(stat.st_size / (1024*1024), 2)
                                })
                            except Exception:
                                pass
                
                current_files.sort(key=lambda x: x['ts'], reverse=True)
                with open(CACHE_FILE, 'w') as f:
                    json.dump(current_files, f)
                    
        except Exception:
            pass
        
        time.sleep(60)

threading.Thread(target=background_processor, daemon=True).start()

# --- EMAIL LOGIC ---
def send_link_email(new_url):
    try:
        recipient = ""
        if os.path.exists(RECIPIENT_FILE):
            with open(RECIPIENT_FILE, 'r') as f: recipient = f.read().strip()
        
        if not recipient: return

        msg = MIMEText(f"Mission Control Online.\n\nNew Link: {new_url}")
        msg['Subject'] = "Mission Control: Link Update"
        msg['From'] = ICLOUD_USER
        msg['To'] = recipient

        with smtplib.SMTP("smtp.mail.me.com", 587) as server:
            server.starttls()
            server.login(ICLOUD_USER, ICLOUD_PASS)
            server.sendmail(ICLOUD_USER, recipient, msg.as_string())
    except: pass

def tunnel_monitor():
    while True:
        try:
            if os.path.exists(TUNNEL_LOG):
                with open(TUNNEL_LOG, 'r') as f:
                    matches = re.findall(r'https://[\w-]+\.trycloudflare\.com', f.read())
                    if matches:
                        current_url = matches[-1]
                        last_url = ""
                        if os.path.exists(LAST_LINK_FILE):
                            with open(LAST_LINK_FILE, 'r') as lf: last_url = lf.read().strip()
                        
                        if current_url != last_url:
                            send_link_email(current_url)
                            with open(LAST_LINK_FILE, 'w') as lf: lf.write(current_url)
        except: pass
        time.sleep(60)

threading.Thread(target=tunnel_monitor, daemon=True).start()

# --- SATELLITE SCHEDULE LOGIC ---
def get_passes():
    now_utc = datetime.datetime.utcnow()
    current_epoch = time.time()
    passes = {}
    
    if now_utc.minute >= 45:
        fy_target = (now_utc + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    else:
        fy_target = now_utc.replace(minute=0, second=0, microsecond=0)
    passes["fy_svissr"] = {
        "interval": "(Hourly)",
        "ts": current_epoch + (fy_target - now_utc).total_seconds(),
        "active": not (45 <= now_utc.minute <= 58)
    }
    
    l3_hours = [0, 3, 6, 9, 12, 15, 18, 21]
    t = now_utc
    for _ in range(24):
        if t.hour in l3_hours:
            target_l3 = t.replace(minute=12, second=0, microsecond=0)
            if target_l3 > now_utc or 0 <= (now_utc - target_l3).total_seconds() < 900: 
                passes["l3_hrit"] = {
                    "interval": "(Every 3 Hours)",
                    "ts": current_epoch + (target_l3 - now_utc).total_seconds(),
                    "active": 0 <= (now_utc - target_l3).total_seconds() < 900
                }
                break
        t = (t + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    target_12 = now_utc.replace(minute=12, second=0, microsecond=0)
    target_42 = now_utc.replace(minute=42, second=0, microsecond=0)
    targets = [target_12, target_42, (now_utc + datetime.timedelta(hours=1)).replace(minute=12, second=0, microsecond=0)]
    
    for tgt in targets:
        if tgt > now_utc or 0 <= (now_utc - tgt).total_seconds() < 900:
            passes["l5_hrit"] = {
                "interval": "(Every 30 Min)",
                "ts": current_epoch + (tgt - now_utc).total_seconds(),
                "active": 0 <= (now_utc - tgt).total_seconds() < 900
            }
            break
            
    return passes

# --- ROUTES ---
@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/data')
def get_data():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp = round(int(f.read()) / 1000, 1)
    except: temp = 0
    total, used, free = shutil.disk_usage("/")
    
    def check(name):
        try:
            return subprocess.run(["tmux", "has-session", "-t", name], 
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        except: return False
        
    def get_tunnel_url():
        if not os.path.exists(TUNNEL_LOG): return "Searching..."
        try:
            with open(TUNNEL_LOG, 'r') as f:
                matches = re.findall(r'https://[\w-]+\.trycloudflare\.com', f.read())
                return matches[-1] if matches else "Searching..."
        except: return "Log Error"

    return jsonify({
        "system": {
            "temp": temp,
            "disk": {
                "percent": round((used / total) * 100, 1),
                "used_gb": round(used / (2**30), 1),
                "total_gb": round(total / (2**30), 1)
            },
            "tunnel_url": get_tunnel_url()
        },
        "passes": get_passes(),
        "tasks": {
            "sync": check("sat-sync"),
            "align": check("alignment"),
            "capture": check("capture")
        }
    })

@app.route('/api/files')
def get_files():
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r') as f:
                return jsonify(json.load(f))
        except: pass
    return jsonify([])

@app.route('/api/settings', methods=['POST'])
def save_settings():
    email = request.json.get('email')
    with open(RECIPIENT_FILE, 'w') as f: f.write(email)
    return jsonify({"status": "ok"})

@app.route('/api/control', methods=['POST'])
def control():
    data = request.json
    if str(data.get('pin')) != ADMIN_PIN:
        return jsonify({"status": "error", "message": "Incorrect PIN"}), 403

    action = data.get('action')
    target = data.get('target')

    if action == "start_capture":
        config = data.get('config', {})
        pipeline = config.get('pipeline', 'elektro_hrit')
        freq = config.get('freq', '1691000000')
        samplerate = config.get('samplerate', '2048000')
        gain = config.get('gain', '45')
        cmd = f"tmux new-session -d -s capture 'satdump live {pipeline} {BASE_DIR} --source rtlsdr --frequency {freq} --samplerate {samplerate} --gain {gain} --bias'"
        subprocess.Popen(cmd, shell=True)
    elif action == "start_align":
        subprocess.Popen("tmux new-session -d -s alignment 'rtl_tcp -a 0.0.0.0'", shell=True)
    elif action == "start_sync":
        cmd = f"tmux new-session -d -s sat-sync 'while true; do rclone move {CAPTURE_DIR} iclouddrive:SatImages -P --delete-empty-src-dirs; sleep 300; done'"
        subprocess.Popen(cmd, shell=True)
    elif action == "stop":
        subprocess.Popen(f"tmux kill-session -t {target}", shell=True)
    elif action == "delete":
        path = os.path.join(CLOUD_VIEW_DIR, data.get('filename'))
        if os.path.exists(path): os.remove(path)
        
    return jsonify({"status": "ok"})

@app.route('/api/logs/<session>')
def get_logs(session):
    try:
        check = subprocess.run(["tmux", "has-session", "-t", session], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode != 0: return jsonify({"log": "OFFLINE"})
        res = subprocess.check_output(["tmux", "capture-pane", "-pt", session, "-S", "-50"])
        return jsonify({"log": res.decode('utf-8', errors='ignore')})
    except: return jsonify({"log": "Error reading logs"})

@app.route('/thumb/<path:filename>')
def serve_thumb(filename):
    safe_name = filename.replace("/", "_")
    thumb_path = os.path.join(THUMB_DIR, safe_name)
    
    if os.path.exists(thumb_path):
        return send_file(thumb_path, mimetype='image/jpeg')
        
    original_path = os.path.join(CLOUD_VIEW_DIR, filename)
    if not os.path.exists(original_path): return "Not found", 404
        
    with thumb_lock:
        if os.path.exists(thumb_path):
            return send_file(thumb_path, mimetype='image/jpeg')
            
        try:
            with Image.open(original_path) as img:
                img = make_jpeg_safe(img) # MUST BE FIRST!
                img.thumbnail((1200, 1200)) 
                img.save(thumb_path, "JPEG", quality=85)
            return send_file(thumb_path, mimetype='image/jpeg')
        except:
            return send_from_directory(CLOUD_VIEW_DIR, filename)

@app.route('/images/<path:filename>')
def serve_image(filename): 
    return send_from_directory(CLOUD_VIEW_DIR, filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
