# 🛰️ Electro-L5 Mission Control (Dubai Ground Station)

A cyberpunk-themed web interface for controlling a Raspberry Pi Satellite Ground Station. Designed for tracking and decoding **Electro-L3/L5** (76°E) HRIT weather data.

![Status](https://img.shields.io/badge/Status-Mission%20Ready-success)
![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi-red)

## 🖥️ Features
* **Real-Time Dashboard:** Monitor CPU temp, Disk usage, and Satellite Timers.
* **Command Center:** Start/Stop `rtl_tcp` alignment and `SatDump` captures with one click.
* **File Explorer:** View decoded satellite imagery directly in the browser.
* **Auto-Sync:** Integration with `rclone` to push images to iCloud/Cloud Storage.
* **Matrix CLI:** A stylized background terminal for that "War Room" aesthetic.

## 🚀 Installation

1.  **Clone the Repo**
    ```bash
    git clone [https://github.com/Geekshere/SatDump-Elektro-L5-WebGUI.git](https://github.com/Geekshere/SatDump-Elektro-L5-WebGUI.git)
    cd SatDump-Elektro-L5-WebGUI
    ```

2.  **Set up the Environment**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    ```

3.  **Run Mission Control**
    ```bash
    python app.py
    ```
    *Access via: http://[YOUR_PI_IP]:5000*

## 🛠️ Configuration
Edit `app.py` to point to your specific `SatDump` output directory:
```python
BASE_DIR = os.path.expanduser("~/SatDump/build/elektro_l3_output")
