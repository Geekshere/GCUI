import os
import subprocess
import shutil
from flask import Flask, render_template, jsonify, request, send_from_directory

app = Flask(__name__)

# --- CONFIGURATION ---
BASE_DIR = os.path.expanduser("~/SatDump/build/elektro_l3_output")
IMAGE_DIR = os.path.join(BASE_DIR, "images")
os.makedirs(IMAGE_DIR, exist_ok=True)

def get_cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return round(int(f.read()) / 1000, 1)
    except:
        return 0

def get_disk_usage():
    total, used, free = shutil.disk_usage("/")
    return {
        "total": round(total / (2**30), 1),
        "used": round(used / (2**30), 1),
        "percent": round((used / total) * 100, 1)
    }

def is_tmux_running(session_name):
    try:
        res = subprocess.run(["tmux", "has-session", "-t", session_name], 
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return res.returncode == 0
    except:
        return False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def status():
    return jsonify({
        "cpu_temp": get_cpu_temp(),
        "disk": get_disk_usage(),
        "sessions": {
            "sync": is_tmux_running("sat-sync"),
            "align": is_tmux_running("alignment"),
            "capture": is_tmux_running("capture")
        }
    })

@app.route('/api/logs/<session>')
def get_logs(session):
    # Reads the last 50 lines of the tmux session
    if not is_tmux_running(session):
        return jsonify({"log": "SESSION OFFLINE"})
    try:
        # capture-pane outputs the text currently on screen
        res = subprocess.check_output(["tmux", "capture-pane", "-pt", session, "-S", "-50"])
        return jsonify({"log": res.decode('utf-8', errors='ignore')})
    except Exception as e:
        return jsonify({"log": str(e)})

@app.route('/api/images')
def get_images():
    if not os.path.exists(IMAGE_DIR):
        return jsonify([])
    files = [f for f in os.listdir(IMAGE_DIR) if f.endswith(('.png', '.jpg', '.jpeg'))]
    files.sort(key=lambda x: os.path.getmtime(os.path.join(IMAGE_DIR, x)), reverse=True)
    return jsonify(files)

@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename)

@app.route('/api/control', methods=['POST'])
def control():
    data = request.json
    action = data.get('action')
    
    if action == "start_sync":
        cmd = f"tmux new-session -d -s sat-sync 'while true; do rclone sync {IMAGE_DIR} iclouddrive:SatImages -P; sleep 300; done'"
        subprocess.Popen(cmd, shell=True)
        
    elif action == "start_align":
        cmd = "tmux new-session -d -s alignment 'rtl_tcp -a 0.0.0.0'"
        subprocess.Popen(cmd, shell=True)
        
    elif action == "start_capture":
        # HRIT Command
        cmd = f"tmux new-session -d -s capture 'satdump live elektro_hrit {BASE_DIR} --source rtlsdr --frequency 1691000000 --samplerate 2048000 --gain 45 --bias'"
        subprocess.Popen(cmd, shell=True)
        
    elif action == "stop_session":
        target = data.get('target')
        if target in ['sat-sync', 'alignment', 'capture']:
            subprocess.Popen(f"tmux kill-session -t {target}", shell=True)

    return jsonify({"status": "executed"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
