import os
import subprocess
import shutil
import datetime
import re
from flask import Flask, render_template, jsonify, request, send_from_directory

app = Flask(__name__)

# --- CONFIG ---
BASE_DIR = os.path.expanduser("~/SatDump/build/elektro_l3_output")
IMAGE_DIR = os.path.join(BASE_DIR, "images")
TUNNEL_LOG = os.path.expanduser("~/mission_control/tunnel.log")
os.makedirs(IMAGE_DIR, exist_ok=True)

# Electro-L Schedule (UTC+4/GST)
SCHEDULE_WINDOWS = [1, 4, 7, 10, 13, 16, 19, 22]
TX_MINUTE = 42

def get_next_pass():
    now = datetime.datetime.now()
    # Check next 24h
    for day_offset in [0, 1]:
        base = now + datetime.timedelta(days=day_offset)
        for h in SCHEDULE_WINDOWS:
            target = base.replace(hour=h, minute=TX_MINUTE, second=0, microsecond=0)
            if target > now:
                diff = target - now
                hrs = int(diff.total_seconds() // 3600)
                mins = int((diff.total_seconds() % 3600) // 60)
                return {
                    "absolute": target.strftime("%H:%M"),
                    "relative": f"{hrs}h {mins}m",
                    "timestamp": target.timestamp()
                }
    return {"absolute": "--:--", "relative": "--", "timestamp": 0}

def get_tunnel_url():
    # Scrapes the log file for the latest .trycloudflare.com link
    if not os.path.exists(TUNNEL_LOG): return "Tunnel Offline"
    try:
        with open(TUNNEL_LOG, 'r') as f:
            content = f.read()
            # Regex to find the URL
            match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', content)
            return match.group(0) if match else "Searching..."
    except: return "Log Error"

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/data')
def get_data():
    # 1. System Stats
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp = round(int(f.read()) / 1000, 1)
    except: temp = 0
    total, used, free = shutil.disk_usage("/")
    
    # 2. Tasks
    def check(name):
        try:
            return subprocess.run(["tmux", "has-session", "-t", name], 
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        except: return False

    return jsonify({
        "system": {
            "temp": temp,
            "disk_percent": round((used / total) * 100, 1),
            "tunnel_url": get_tunnel_url()
        },
        "next_pass": get_next_pass(),
        "tasks": {
            "sync": check("sat-sync"),
            "align": check("alignment"),
            "capture": check("capture")
        }
    })

@app.route('/api/calendar')
def get_calendar_data():
    # Returns a list of dates that have images
    if not os.path.exists(IMAGE_DIR): return jsonify([])
    dates = set()
    for f in os.listdir(IMAGE_DIR):
        if f.endswith(('.png', '.jpg')):
            # Assuming file has timestamp or we use file stats
            ts = os.path.getmtime(os.path.join(IMAGE_DIR, f))
            date_str = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
            dates.add(date_str)
    return jsonify(list(dates))

@app.route('/api/control', methods=['POST'])
def control():
    data = request.json
    action = data.get('action')
    
    if action == "start_capture":
        # HRIT Capture
        cmd = f"tmux new-session -d -s capture 'satdump live elektro_hrit {BASE_DIR} --source rtlsdr --frequency 1691000000 --samplerate 2048000 --gain 45 --bias'"
        subprocess.Popen(cmd, shell=True)
    elif action == "start_align":
        # RTL TCP
        subprocess.Popen("tmux new-session -d -s alignment 'rtl_tcp -a 0.0.0.0'", shell=True)
    elif action == "start_sync":
        # iCloud Sync
        cmd = f"tmux new-session -d -s sat-sync 'while true; do rclone sync {IMAGE_DIR} iclouddrive:SatImages -P; sleep 300; done'"
        subprocess.Popen(cmd, shell=True)
    elif action == "stop":
        subprocess.Popen(f"tmux kill-session -t {data.get('target')}", shell=True)
        
    return jsonify({"status": "ok"})

@app.route('/images/<path:filename>')
def serve_image(filename): return send_from_directory(IMAGE_DIR, filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
