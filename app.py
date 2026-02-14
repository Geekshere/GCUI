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
ADMIN_PIN = "9494"

os.makedirs(IMAGE_DIR, exist_ok=True)

# Electro-L5 Schedule: Standard MSU-GS imaging is every 30 mins.
# HRIT typically transmits at XX:12 and XX:42.
TRANSMISSION_MINUTES = [12, 42]

def get_next_pass():
    now = datetime.datetime.now()
    # Search next 24 hours for 30-min windows
    for day_offset in [0, 1]:
        base = now + datetime.timedelta(days=day_offset)
        for hour in range(0, 24):
            for minute in TRANSMISSION_MINUTES:
                target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if target > now:
                    diff = target - now
                    hrs = int(diff.total_seconds() // 3600)
                    mins = int((diff.total_seconds() % 3600) // 60)
                    return {
                        "absolute": target.strftime("%H:%M"),
                        "relative": f"{hrs}h {mins}m"
                    }
    return {"absolute": "--:--", "relative": "--"}

def get_tunnel_url():
    if not os.path.exists(TUNNEL_LOG): return "Tunnel Offline"
    try:
        with open(TUNNEL_LOG, 'r') as f:
            content = f.read()
            match = re.search(r'https://[a-zA-Z0-9-]+\.trycloudflare\.com', content)
            return match.group(0) if match else "Searching..."
    except: return "Log Error"

@app.route('/')
def index(): return render_template('index.html')

@app.route('/api/data')
def get_data():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            # KEEP DECIMAL: Round to 1 decimal place
            temp = round(int(f.read()) / 1000, 1)
    except: temp = 0
    total, used, free = shutil.disk_usage("/")
    
    def check(name):
        try:
            return subprocess.run(["tmux", "has-session", "-t", name], 
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0
        except: return False

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
        "next_pass": get_next_pass(),
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

@app.route('/api/control', methods=['POST'])
def control():
    data = request.json
    if str(data.get('pin')) != ADMIN_PIN:
        return jsonify({"status": "error", "message": "Incorrect PIN"}), 403

    action = data.get('action')
    target = data.get('target')

    if action == "start_capture":
        cmd = f"tmux new-session -d -s capture 'satdump live elektro_hrit {BASE_DIR} --source rtlsdr --frequency 1691000000 --samplerate 2048000 --gain 45 --bias'"
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
    app.run(host='0.0.0.0', port=5000, debug=True)
