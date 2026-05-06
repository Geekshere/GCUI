Quick edit: since uploading this i have improced it alot, but have not had the time to add it to github, so if anyone is interested let me know and i can update it


# 🛰️ Mission Control: Dubai Satellite Ground Station

A high-performance, slate-themed web dashboard for controlling a Raspberry Pi Satellite Ground Station. Originally built for Elektro-L, now expanded for high-fidelity **FengYun-2H** and **Elektro-L3/L5** geostationary data reception.

![Status](https://img.shields.io/badge/Status-Mission%20Ready-success)
![SNR](https://img.shields.io/badge/Peak%20SNR-7.18%20dB-blue)
![Platform](https://img.shields.io/badge/Platform-Raspberry%20Pi-red)

## 🖥️ New Features
* **Multi-Satellite Tracking:** Real-time acquisition timers for FengYun-2H, Elektro-L3, and Elektro-L5.
* **Recursive Archive:** Deep-folder scanning logic that automatically organizes and displays images by capture date/time.
* **Smart Timelapse:** Built-in playback engine with channel-specific filtering (e.g., FC, IR, VIS) and custom speed controls.
* **Command Center:** Secure PIN-protected controls for `rtl_tcp` alignment, `SatDump` live decoding, and Cloud synchronization.
* **Robust Sharing:** Adaptive "Share/Download" logic for seamless image export on iOS, macOS, and Desktop browsers.

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
    pip install flask
    ```

3.  **Run Mission Control**
    ```bash
    python app.py
    ```
    *Access via: http://[YOUR_PI_IP]:5000 or your Cloudflare Tunnel URL.*

## 🛠️ Configuration
The system is optimized for the following directory structure:
```python
BASE_DIR = os.path.expanduser("~/SatDump/build/elektro_l3_output")
IMAGE_DIR = os.path.join(BASE_DIR, "IMAGE/")
