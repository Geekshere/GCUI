import os
import subprocess
import shutil
import datetime
import re
import smtplib
import threading
import time
from email.mime.text import MIMEText
from flask import Flask, render_template, jsonify, request, send_from_directory

app = Flask(__name__)

# --- CONFIG ---
BASE_DIR = os.path.expanduser("~/SatDump/build/elektro_l3_output")
IMAGE_DIR = os.path.join(BASE_DIR, "IMAGE/")
TUNNEL_LOG = "/home/ethan/mission_control/tunnel.log"
LAST_LINK_FILE = "/home/ethan/mission_control/last_link.txt"
RECIPIENT_FILE = "/home/ethan/mission_control/recipient.txt"
ADMIN_PIN = "9494"

# ICLOUD CONFIG
ICLOUD_USER = "ethangotz@icloud.com"
ICLOUD_PASS = "ctaf-ktli-ilth-ewvg"

os.makedirs(IMAGE_DIR, exist_ok=True)

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
    
    # 1. FY-2H: Hourly, starts at XX:00, dark 45-58
    if now_utc.minute >= 45:
        fy_target = (now_utc + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    else:
        fy_target = now_utc.replace(minute=0, second=0, microsecond=0)
    passes["fy_svissr"] = {
        "interval": "(Hourly)",
        "ts": current_epoch + (fy_target - now_utc).total_seconds(),
        "active": not (45 <= now_utc.minute <= 58)
    }
    
    # 2. L3: Every 3 hours at XX:12 UTC (00:12, 03:12, 06:12, etc.)
    l3_hours = [0, 3, 6, 9, 12, 15, 18, 21]
    t = now_utc
    for _ in range(24):
        if t.hour in l3_hours:
            target_l3 = t.replace(minute=12, second=0, microsecond=0)
            if target_l3 > now_utc or 0 <= (now_utc - target_l3).total_seconds() < 900: # 15 min active window
                passes["l3_hrit"] = {
                    "interval": "(Every 3 Hours)",
                    "ts": current_epoch + (target_l3 - now_utc).total_seconds(),
                    "active": 0 <= (now_utc - target_l3).total_seconds() < 900
                }
                break
        t = (t + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)

    # 3. L5: Every 30 mins at XX:12 and XX:42
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
    if not os.path.exists(IMAGE_DIR): return jsonify([])
    files = []
    try:
        all_f = sorted(os.listdir(IMAGE_DIR), key=lambda x: os.path.getmtime(os.path.join(IMAGE_DIR, x)), reverse=True)
        for f in all_f:
            if f.endswith(('.png', '.jpg', '.jpeg')):
                path = os.path.join(IMAGE_DIR, f)
                stat = os.stat(path)
                files.append({
                    "name": f,
                    "ts": stat.st_mtime,
                    "size_mb": round(stat.st_size / (1024*1024), 2)
                })
    except: pass
    return jsonify(files)

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
        cmd = f"tmux new-session -d -s sat-sync 'while true; do rclone sync {IMAGE_DIR} iclouddrive:SatImages -P; sleep 300; done'"
        subprocess.Popen(cmd, shell=True)
    elif action == "stop":
        subprocess.Popen(f"tmux kill-session -t {target}", shell=True)
    elif action == "delete":
        path = os.path.join(IMAGE_DIR, data.get('filename'))
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

@app.route('/images/<path:filename>')
def serve_image(filename): return send_from_directory(IMAGE_DIR, filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True, use_reloader=False)
