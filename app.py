import os
import subprocess
import shutil
import datetime
from flask import Flask, render_template, jsonify, request, send_from_directory

app = Flask(__name__)

# --- CONFIGURATION ---
BASE_DIR = os.path.expanduser("~/SatDump/build/elektro_l3_output")
IMAGE_DIR = os.path.join(BASE_DIR, "images")
os.makedirs(IMAGE_DIR, exist_ok=True)

# Electro-L transmits HRIT roughly every 3 hours. 
# GST is UTC+4. These are the expected GST windows.
SCHEDULE_WINDOWS_GST = [1, 4, 7, 10, 13, 16, 19, 22] 
TRANSMISSION_MINUTE = 42

def get_next_passes():
    now = datetime.datetime.now()
    passes = []
    # Check today and tomorrow
    for day_offset in [0, 1]:
        date_base = now + datetime.timedelta(days=day_offset)
        for hour in SCHEDULE_WINDOWS_GST:
            pass_time = date_base.replace(hour=hour, minute=TRANSMISSION_MINUTE, second=0, microsecond=0)
            if pass_time > now:
                passes.append(pass_time)
    
    # Return next 3 passes formatted
    result = []
    for p in passes[:3]:
        diff = p - now
        hours = int(diff.total_seconds() // 3600)
        mins = int((diff.total_seconds() % 3600) // 60)
        result.append({
            "time_str": p.strftime("%H:%M GST"),
            "countdown": f"{hours}h {mins}m"
        })
    return result

def get_system_stats():
    # CPU Temp
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            temp = round(int(f.read()) / 1000, 1)
    except:
        temp = 0
    # Disk
    total, used, free = shutil.disk_usage("/")
    return {
        "cpu": temp,
        "disk_percent": round((used / total) * 100, 1),
        "disk_text": f"{round(used / (2**30), 1)}GB / {round(total / (2**30), 1)}GB"
    }

def is_tmux_running(session):
    try:
        res = subprocess.run(["tmux", "has-session", "-t", session], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except:
        return False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/dashboard')
def dashboard_data():
    return jsonify({
        "stats": get_system_stats(),
        "schedule": get_next_passes(),
        "tasks": {
            "sync": is_tmux_running("sat-sync"),
            "align": is_tmux_running("alignment"),
            "capture": is_tmux_running("capture")
        }
    })

@app.route('/api/files')
def list_files():
    if not os.path.exists(IMAGE_DIR): return jsonify([])
    files = []
    for f in os.listdir(IMAGE_DIR):
        path = os.path.join(IMAGE_DIR, f)
        if os.path.isfile(path) and f.endswith(('.png', '.jpg', '.jpeg')):
            stat = os.stat(path)
            files.append({
                "name": f,
                "size": f"{round(stat.st_size / (1024*1024), 2)} MB",
                "date": datetime.datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                "timestamp": stat.st_mtime
            })
    # Sort new to old
    return jsonify(sorted(files, key=lambda x: x['timestamp'], reverse=True))

@app.route('/api/delete', methods=['POST'])
def delete_file():
    filename = request.json.get('filename')
    path = os.path.join(IMAGE_DIR, filename)
    if os.path.exists(path) and ".." not in filename: # Security check
        os.remove(path)
        return jsonify({"status": "deleted"})
    return jsonify({"status": "error"})

@app.route('/api/control', methods=['POST'])
def control():
    action = request.json.get('action')
    if action == "start_capture":
        cmd = f"tmux new-session -d -s capture 'satdump live elektro_hrit {BASE_DIR} --source rtlsdr --frequency 1691000000 --samplerate 2048000 --gain 45 --bias'"
        subprocess.Popen(cmd, shell=True)
    elif action == "start_align":
        subprocess.Popen("tmux new-session -d -s alignment 'rtl_tcp -a 0.0.0.0'", shell=True)
    elif action == "start_sync":
        cmd = f"tmux new-session -d -s sat-sync 'while true; do rclone sync {IMAGE_DIR} iclouddrive:SatImages -P; sleep 300; done'"
        subprocess.Popen(cmd, shell=True)
    elif action == "stop":
        target = request.json.get('target')
        subprocess.Popen(f"tmux kill-session -t {target}", shell=True)
    return jsonify({"status": "ok"})

@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
