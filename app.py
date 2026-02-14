import os
import time
import subprocess
import shutil
import psutil
from flask import Flask, render_template, jsonify, request, send_from_directory

app = Flask(__name__)

# --- CONFIGURATION ---
# Pointing to your specific build folder
BASE_DIR = os.path.expanduser("~/SatDump/build/elektro_l3_output")
IMAGE_DIR = os.path.join(BASE_DIR, "images")

# Ensure directories exist so the dashboard doesn't crash
os.makedirs(IMAGE_DIR, exist_ok=True)

def get_cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
            return round(int(f.read()) / 1000, 1)
    except:
        return 0

def get_disk_details():
    # Returns precise GB usage
    total, used, free = shutil.disk_usage("/")
    return {
        "total": round(total / (2**30), 1),
        "used": round(used / (2**30), 1),
        "percent": round((used / total) * 100, 1)
    }

def is_tmux_running(session_name):
    try:
        result = subprocess.run(["tmux", "has-session", "-t", session_name], 
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except:
        return False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/api/status')
def status():
    return jsonify({
        "cpu_temp": get_cpu_temp(),
        "disk": get_disk_details(),
        "sync_status": is_tmux_running("sat-sync"),
        "align_status": is_tmux_running("alignment"),
        "capture_status": is_tmux_running("capture")
    })

@app.route('/api/files')
def list_files():
    # Lists files in the output directory for the explorer
    file_list = []
    try:
        for f in os.listdir(IMAGE_DIR):
            path = os.path.join(IMAGE_DIR, f)
            if os.path.isfile(path):
                size_mb = round(os.path.getsize(path) / (1024 * 1024), 2)
                file_list.append({"name": f, "size": size_mb})
    except Exception as e:
        return jsonify({"error": str(e)})
    
    # Sort by name (or you could sort by time)
    return jsonify(sorted(file_list, key=lambda x: x['name']))

@app.route('/api/images')
def get_images():
    if not os.path.exists(IMAGE_DIR):
        return jsonify([])
    files = [f for f in os.listdir(IMAGE_DIR) if f.endswith(('.png', '.jpg', '.jpeg'))]
    # Sort by modification time (newest first)
    files.sort(key=lambda x: os.path.getmtime(os.path.join(IMAGE_DIR, x)), reverse=True)
    return jsonify(files)

@app.route('/images/<path:filename>')
def serve_image(filename):
    return send_from_directory(IMAGE_DIR, filename)

@app.route('/api/control', methods=['POST'])
def control():
    data = request.json
    action = data.get('action')
    
    # These commands run strictly in the system shell, so venv doesn't affect tmux itself
    if action == "start_sync":
        cmd = f"tmux new-session -d -s sat-sync 'while true; do rclone sync {IMAGE_DIR} iclouddrive:SatImages -P; sleep 300; done'"
        subprocess.Popen(cmd, shell=True)
        
    elif action == "start_align":
        cmd = "tmux new-session -d -s alignment 'rtl_tcp -a 0.0.0.0'"
        subprocess.Popen(cmd, shell=True)
        
    elif action == "start_capture":
        # The HRIT command
        cmd = f"tmux new-session -d -s capture 'satdump live elektro_hrit {BASE_DIR} --source rtlsdr --frequency 1691000000 --samplerate 2048000 --gain 45 --bias'"
        subprocess.Popen(cmd, shell=True)
        
    elif action == "stop_all":
        subprocess.Popen("tmux kill-session -t sat-sync", shell=True)
        subprocess.Popen("tmux kill-session -t alignment", shell=True)
        subprocess.Popen("tmux kill-session -t capture", shell=True)

    return jsonify({"status": "executed"})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
