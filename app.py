import os, shutil, datetime, re, threading, time, json, sqlite3, subprocess
import secrets as _sec, hashlib, random, smtplib, base64
from contextlib import contextmanager
import requests
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from flask import (Flask, render_template, jsonify, request,
                   send_from_directory, send_file, make_response)
from PIL import Image

Image.MAX_IMAGE_PIXELS = None
app = Flask(__name__)

# -----------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------
# Most values below can be overridden with environment variables so you
# never have to hardcode secrets in this file. See .env.example / README.
PI_IP         = os.environ.get("PI_IP", "192.168.1.100")
PI_API        = f"http://{PI_IP}:5001/api"
BASE_DATA_DIR = "/app/data"
CLOUD_VIEW    = os.path.join(BASE_DATA_DIR, "images")
THUMB_DIR     = os.path.join(BASE_DATA_DIR, "thumbs")
AVATAR_DIR    = os.path.join(BASE_DATA_DIR, "avatars")
CACHE_FILE    = os.path.join(BASE_DATA_DIR, "files_cache.json")
PI_CACHE_FILE = os.path.join(BASE_DATA_DIR, "pi_cache.json")
DB_FILE       = os.path.join(BASE_DATA_DIR, "station.db")
ADMIN_PIN     = os.environ.get("ADMIN_PIN", "0000")

# Owner account seeded into the DB on first run. Set these via environment
# variables (e.g. in your docker-compose.yaml or an untracked .env file) —
# do NOT hardcode real credentials here, especially before pushing to a
# public repo.
OWNER_EMAIL    = os.environ.get("OWNER_EMAIL", "owner@example.com")
OWNER_USERNAME = os.environ.get("OWNER_USERNAME", "Owner")
OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD", "changeme")

# Shown in the "Questions, bugs, or support" footer at the bottom of pages
CONTACT_EMAIL  = os.environ.get("CONTACT_EMAIL", "you@example.com")

for d in (CLOUD_VIEW, THUMB_DIR, AVATAR_DIR):
    os.makedirs(d, exist_ok=True)

PI_USER       = os.environ.get("PI_USER", "pi")  # kept for reference; not currently used by any route

# -----------------------------------------------------------------------
# Background Pi data fetcher (caches to disk so /api/data is instant)
# -----------------------------------------------------------------------
_pi_cache = {"temp":0,"disk":{"percent":0,"used_gb":0,"total_gb":0},"tasks":{"sync":False,"align":False,"capture":False},"online":False,"last_seen":0}

def _load_pi_cache():
    global _pi_cache
    try:
        with open(PI_CACHE_FILE) as f: _pi_cache=json.load(f)
    except Exception: pass

_load_pi_cache()


_img_size_cache = 0.0

def _update_img_size():
    """Background thread: recalculate image storage size every 10 min."""
    global _img_size_cache
    while True:
        try:
            total = 0.0
            for root, dirs, files in os.walk(CLOUD_VIEW):
                for fn in files:
                    if fn.lower().endswith(('.jpg', '.jpeg', '.png')):
                        try: total += os.path.getsize(os.path.join(root, fn))
                        except Exception: pass
            _img_size_cache = round(total / 2**30, 2)
        except Exception: pass
        time.sleep(600)

threading.Thread(target=_update_img_size, daemon=True).start()

def pi_fetcher():
    global _pi_cache
    while True:
        try:
            r=requests.get('%s/data'%PI_API,timeout=4).json()
            sys_info=r.get('system',{})
            temp=sys_info.get('temp',0)
            _pi_cache={
                "temp":temp,
                "disk":sys_info.get('disk',{"percent":0,"used_gb":0,"total_gb":0}),
                "tasks":r.get('tasks',{"sync":False,"align":False,"capture":False}),
                "online":True,
                "last_seen":int(time.time())
            }
            with open(PI_CACHE_FILE,'w') as f: json.dump(_pi_cache,f)
            # Store peak temp reading
            if temp>0:
                try:
                    with get_db() as c:
                        c.execute('INSERT INTO peak_temps (temp,ts) VALUES (?,?)',(temp,time.time()))
                        # Purge readings older than 48h
                        c.execute('DELETE FROM peak_temps WHERE ts<?',(time.time()-172800,))
                except Exception: pass
        except Exception:
            _pi_cache["online"] = False
            try:
                with open(PI_CACHE_FILE,'w') as f: json.dump(_pi_cache,f)
            except Exception: pass
        time.sleep(30)

threading.Thread(target=pi_fetcher,daemon=True).start()

SAT_WINDOWS = {
    "l3_hrit":   [(12, 27)],
    "l5_hrit":   [(12, 27), (42, 57)],
}
ACTIVE_SAT = "fy_svissr"

# -----------------------------------------------------------------------
# Bad word filter
# -----------------------------------------------------------------------
BAD_WORDS = {
    'fuck','shit','bitch','bastard','cunt','dick','cock','pussy',
    'nigger','nigga','faggot','retard','whore','slut','twat','wanker',
    'asshole','motherfucker','bullshit','arse','bollocks','piss',
}

def clean(text):
    words = text.split()
    return ' '.join('*'*len(w) if re.sub(r'[^a-z0-9]','',w.lower()) in BAD_WORDS else w for w in words)

# -----------------------------------------------------------------------
# Captcha
# -----------------------------------------------------------------------
_captchas = {}
_cap_lock = threading.Lock()

def new_captcha():
    a, b = random.randint(2,15), random.randint(2,15)
    token = _sec.token_hex(16)
    with _cap_lock:
        expired = [k for k,v in _captchas.items() if v[1]<time.time()]
        for k in expired: del _captchas[k]
        _captchas[token] = (a+b, time.time()+300)
    return token, 'What is %d + %d?' % (a, b)

def check_captcha(token, answer):
    with _cap_lock:
        entry = _captchas.get(token)
        if not entry: return False
        exp, expires = entry
        if time.time() > expires: del _captchas[token]; return False
        del _captchas[token]
    try: return int(answer) == exp
    except: return False

# -----------------------------------------------------------------------
# Password hashing
# -----------------------------------------------------------------------
def hash_pw(pw, salt=None):
    if not salt: salt = _sec.token_hex(16)
    h = hashlib.sha256((pw+salt).encode()).hexdigest()
    return h, salt

def verify_pw(pw, h, salt):
    return hashlib.sha256((pw+salt).encode()).hexdigest() == h

# -----------------------------------------------------------------------
# Database  -- check_same_thread=False + proper contextmanager close
# -----------------------------------------------------------------------
@contextmanager
def _setup_db():
    """One-time WAL + FK setup - called once at startup."""
    try:
        conn = sqlite3.connect(DB_FILE, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.commit()
        conn.close()
    except Exception as e:
        print('DB setup warning:', e)

@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE, check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

_setup_db()
def init_db():
    with get_db() as c:
        c.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                email         TEXT    NOT NULL UNIQUE,
                username      TEXT    NOT NULL,
                password_hash TEXT    NOT NULL,
                salt          TEXT    NOT NULL,
                avatar        TEXT    DEFAULT NULL,
                is_owner      INTEGER DEFAULT 0,
                email_notify  INTEGER DEFAULT 1,
                created_ts    REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                token      TEXT    NOT NULL UNIQUE,
                expires_ts REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS comments (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                image_name TEXT    NOT NULL,
                user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
                author     TEXT    NOT NULL,
                text       TEXT    NOT NULL,
                parent_id  INTEGER REFERENCES comments(id) ON DELETE CASCADE,
                ts         REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS comment_likes (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                ts         REAL    NOT NULL,
                UNIQUE(comment_id, user_id)
            );
            CREATE TABLE IF NOT EXISTS favorites (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                image_name TEXT    NOT NULL,
                user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                ts         REAL    NOT NULL,
                UNIQUE(image_name, user_id)
            );
            CREATE TABLE IF NOT EXISTS announcements (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                text TEXT    NOT NULL,
                ts   REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                type    TEXT    NOT NULL,
                message TEXT    NOT NULL,
                link    TEXT,
                read    INTEGER DEFAULT 0,
                ts      REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                to_user_id   INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                text         TEXT    NOT NULL,
                read         INTEGER DEFAULT 0,
                ts           REAL    NOT NULL
            );
            CREATE TABLE IF NOT EXISTS mail_config (
                id            INTEGER PRIMARY KEY,
                smtp_email    TEXT,
                smtp_password TEXT,
                enabled       INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS satellite_configs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                sat_key      TEXT    NOT NULL UNIQUE,
                name         TEXT    NOT NULL,
                file_pattern TEXT    NOT NULL,
                active       INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS channel_configs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                sat_key             TEXT    NOT NULL,
                chan_key            TEXT    NOT NULL,
                name                TEXT    NOT NULL,
                description         TEXT    DEFAULT "",
                wavelength          TEXT    DEFAULT "",
                file_pattern        TEXT    NOT NULL,
                example_image       TEXT    DEFAULT NULL,
                delete_threshold_mb REAL    DEFAULT 0,
                sort_order          INTEGER DEFAULT 99,
                UNIQUE(sat_key, chan_key)
            );
            CREATE TABLE IF NOT EXISTS peak_temps (
                id   INTEGER PRIMARY KEY AUTOINCREMENT,
                temp REAL NOT NULL,
                ts   REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS unknown_files (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                filename   TEXT    NOT NULL UNIQUE,
                first_seen REAL    NOT NULL,
                resolved   INTEGER DEFAULT 0
            );
        ''')
        if not c.execute('SELECT 1 FROM mail_config WHERE id=1').fetchone():
            c.execute("INSERT INTO mail_config (id,enabled) VALUES (1,0)")
        if not c.execute('SELECT 1 FROM users WHERE email=?',(OWNER_EMAIL,)).fetchone():
            h, s = hash_pw(OWNER_PASSWORD)
            c.execute(
                'INSERT INTO users (email,username,password_hash,salt,is_owner,created_ts) VALUES (?,?,?,?,1,?)',
                (OWNER_EMAIL, OWNER_USERNAME, h, s, time.time())
            )
        # Seed default satellite configs if empty
        if not c.execute('SELECT 1 FROM satellite_configs').fetchone():
            c.executemany('INSERT OR IGNORE INTO satellite_configs (sat_key,name,file_pattern) VALUES (?,?,?)',[
                ('fy_svissr','FengYun-2H','FY-2'),
                ('l3_hrit','Elektro-L3','L3'),
                ('l5_hrit','Elektro-L5','L5'),
            ])
        # Seed default channel configs if empty
        if not c.execute('SELECT 1 FROM channel_configs').fetchone():
            defaults=[
                ('fy_svissr','FC','Full Color','Composite visible + IR','N/A','_FC_',0,1),
                ('fy_svissr','1','Visible','Visible light channel','0.55-0.90 um','_1_',0,2),
                ('fy_svissr','2','IR1','Infrared thermal window','10.3-11.3 um','_2_',0,3),
                ('fy_svissr','3','WV','Water vapour','6.3-7.6 um','_3_',0,4),
                ('fy_svissr','4','IR2','Split window','11.5-12.5 um','_4_',0,5),
                ('fy_svissr','5','MIR','Mid-infrared','3.5-4.0 um','_5_',0,6),
            ]
            c.executemany('INSERT OR IGNORE INTO channel_configs (sat_key,chan_key,name,description,wavelength,file_pattern,delete_threshold_mb,sort_order) VALUES (?,?,?,?,?,?,?,?)',defaults)

init_db()

# -----------------------------------------------------------------------
# Database migration - add columns that may not exist in old databases
# -----------------------------------------------------------------------
def migrate_db():
    # Rename all existing comments to 'Owner' (one-time migration)
    try:
        with get_db() as c:
            c.execute("UPDATE comments SET author='Owner' WHERE 1=1")
    except Exception: pass

    migrations = [
        # favorites table - add user_id if missing
        "ALTER TABLE favorites ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE CASCADE",
        "ALTER TABLE favorites ADD COLUMN ts REAL NOT NULL DEFAULT 0",
        # Drop session_id NOT NULL constraint by recreating favorites if needed
        # (handled separately below)
        # comments table - add user_id if missing
        "ALTER TABLE comments ADD COLUMN user_id INTEGER REFERENCES users(id) ON DELETE SET NULL",
        "ALTER TABLE comments ADD COLUMN parent_id INTEGER REFERENCES comments(id) ON DELETE CASCADE",
        # Add new tables in case they don't exist yet
        """CREATE TABLE IF NOT EXISTS comment_likes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            ts REAL NOT NULL,
            UNIQUE(comment_id, user_id))""",
        """CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            from_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            to_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            text TEXT NOT NULL, read INTEGER DEFAULT 0, ts REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS satellite_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sat_key TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
            file_pattern TEXT NOT NULL, active INTEGER DEFAULT 1)""",
        """CREATE TABLE IF NOT EXISTS channel_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sat_key TEXT NOT NULL, chan_key TEXT NOT NULL,
            name TEXT NOT NULL, description TEXT DEFAULT '',
            wavelength TEXT DEFAULT '', file_pattern TEXT NOT NULL,
            example_image TEXT DEFAULT NULL,
            delete_threshold_mb REAL DEFAULT 0, sort_order INTEGER DEFAULT 99,
            UNIQUE(sat_key, chan_key))""",
        """CREATE TABLE IF NOT EXISTS peak_temps (
            id INTEGER PRIMARY KEY AUTOINCREMENT, temp REAL NOT NULL, ts REAL NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS unknown_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            filename TEXT NOT NULL UNIQUE, first_seen REAL NOT NULL, resolved INTEGER DEFAULT 0)""",
    ]
    with get_db() as c:
        for sql in migrations:
            try:
                c.execute(sql)
            except Exception:
                pass  # Column/table already exists, that's fine

        # Special fix: if favorites has session_id NOT NULL, recreate without it
        try:
            cols=[row[1] for row in c.execute("PRAGMA table_info(favorites)").fetchall()]
            if 'session_id' in cols:
                c.executescript('''
                    CREATE TABLE IF NOT EXISTS favorites_new (
                        id         INTEGER PRIMARY KEY AUTOINCREMENT,
                        image_name TEXT    NOT NULL,
                        user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                        ts         REAL    NOT NULL DEFAULT 0,
                        UNIQUE(image_name, user_id)
                    );
                    INSERT OR IGNORE INTO favorites_new (id, image_name, user_id, ts)
                        SELECT id, image_name, COALESCE(user_id,0), COALESCE(ts,0)
                        FROM favorites WHERE user_id IS NOT NULL AND user_id > 0;
                    DROP TABLE favorites;
                    ALTER TABLE favorites_new RENAME TO favorites;
                ''')
                print('migrate_db: recreated favorites table without session_id')
        except Exception as e:
            print('migrate_db favorites recreate error:', e)

migrate_db()


SESSION_DAYS = 30

def get_user():
    token = request.cookies.get('st') or request.headers.get('X-ST')
    if not token: return None
    with get_db() as c:
        row = c.execute(
            'SELECT u.* FROM users u JOIN sessions s ON u.id=s.user_id '
            'WHERE s.token=? AND s.expires_ts>?', (token, time.time())
        ).fetchone()
    return dict(row) if row else None

def require_user():
    u = get_user()
    if not u: return None, (jsonify({"status":"error","message":"Login required"}), 401)
    return u, None

def create_session(user_id):
    token = _sec.token_hex(32)
    expires = time.time() + SESSION_DAYS*86400
    with get_db() as c:
        c.execute('INSERT INTO sessions (user_id,token,expires_ts) VALUES (?,?,?)',(user_id,token,expires))
    return token, expires

def user_dict(u):
    return {"id":u["id"],"email":u["email"],"username":u["username"],
            "avatar":u["avatar"],"is_owner":bool(u["is_owner"]),"email_notify":bool(u["email_notify"])}

# -----------------------------------------------------------------------
# Email
# -----------------------------------------------------------------------
def get_mail_cfg():
    with get_db() as c:
        row = c.execute('SELECT * FROM mail_config WHERE id=1').fetchone()
    return dict(row) if row else {}

def send_email(to_email, subject, body_html):
    def _send():
        cfg = get_mail_cfg()
        if not cfg.get('enabled') or not cfg.get('smtp_email'): return
        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject; msg['From'] = cfg['smtp_email']; msg['To'] = to_email
        msg.attach(MIMEText(body_html,'html'))
        try:
            with smtplib.SMTP('smtp.mail.me.com', 587, timeout=10) as s:
                s.starttls(); s.login(cfg['smtp_email'],cfg['smtp_password']); s.send_message(msg)
        except Exception as e: print('Email error:',e)
    threading.Thread(target=_send, daemon=True).start()

def notify_reply(parent_cid, reply_author, reply_text, image_name):
    try:
        with get_db() as c:
            parent = c.execute(
                'SELECT u.id,u.email,u.username,u.email_notify FROM comments cm '
                'JOIN users u ON cm.user_id=u.id WHERE cm.id=?', (parent_cid,)
            ).fetchone()
            if not parent: return
            c.execute('INSERT INTO notifications (user_id,type,message,link,ts) VALUES (?,?,?,?,?)',
                      (parent['id'],'reply','%s replied to your comment' % reply_author,'/comments-feed',time.time()))
        if parent['email_notify']:
            html = ('<p>Hi %s,</p><p><b>%s</b> replied: <em>%s</em></p>'
                    '<p><a href="http://localhost:8080/comments-feed" style="background:#4facfe;color:#fff;padding:10px 20px;border-radius:4px;text-decoration:none">View</a></p>'
                    % (parent['username'],reply_author,reply_text[:200]))
            send_email(parent['email'],'New reply on your comment',html)
    except Exception as e: print('notify_reply error:',e)

# -----------------------------------------------------------------------
# Image helpers
# -----------------------------------------------------------------------
thumb_lock = threading.Lock()
SYNO_SKIP_RE = re.compile(r'^(SYNOFILE_|@eaDir|\.)',re.IGNORECASE)

def make_jpeg_safe(img):
    if img.mode in ('I','I;16','I;16B','I;16L','F'):
        lo,hi = img.getextrema()
        if hi<=lo: hi=lo+1
        img = img.point(lambda x:(x-lo)*(255.0/(hi-lo))).convert('L')
    if img.mode in ('RGBA','LA') or (img.mode=='P' and 'transparency' in img.info):
        bg=Image.new('RGB',img.size,(0,0,0)); bg.paste(img,mask=img.convert('RGBA').split()[-1]); return bg
    return img if img.mode=='RGB' else img.convert('RGB')

def thumb_path(rel):
    return os.path.join(THUMB_DIR, rel.replace('/','_'))

def extract_ts(filename, fallback):
    for pat in (r'(20\d{2})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})',r'(20\d{2})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})'):
        m = re.search(pat,filename)
        if m:
            try:
                dt = datetime.datetime(*(int(x) for x in m.groups()),tzinfo=datetime.timezone.utc)
                return dt.timestamp()
            except Exception: pass
    return fallback

def _scan_files(make_thumbs=True):
    """Scan CLOUD_VIEW, optionally generate missing thumbs, return sorted file list."""
    files = []
    if not os.path.exists(CLOUD_VIEW):
        return files
    for root, dirs, fnames in os.walk(CLOUD_VIEW):
        dirs[:] = [d for d in dirs if not SYNO_SKIP_RE.match(d)]
        for f in fnames:
            if SYNO_SKIP_RE.match(f): continue
            if not f.lower().endswith(('.png','.jpg','.jpeg')): continue
            full = os.path.join(root,f)
            # Skip FY-2 partial frames
            if "FY-2" in f:
                m = re.search(r'T\d{2}(\d{2})\d{2}Z',f)
                if m and 29<=int(m.group(1))<=59:
                    try: os.remove(full)
                    except Exception: pass
                    continue
            rel = os.path.relpath(full,CLOUD_VIEW)
            tp = thumb_path(rel)
            if make_thumbs and not os.path.exists(tp):
                try:
                    with Image.open(full) as img:
                        img=make_jpeg_safe(img); img.thumbnail((1200,1200))
                        img.save(tp,'JPEG',quality=85)
                except Exception: pass
            # Only include in list if thumb exists (handles mid-copy files)
            if not os.path.exists(tp): continue
            try:
                st = os.stat(full)
                files.append({"name":rel,"ts":extract_ts(f,st.st_mtime),"size_mb":round(st.st_size/1048576,2)})
            except Exception: pass
    files.sort(key=lambda x:x['ts'],reverse=True)
    return files

def background_processor():
    # First pass: fast scan (build cache quickly, thumbnails generated inline)
    # Subsequent passes: every 60s to pick up new files
    first_run=True
    while True:
        try:
            files = _scan_files(make_thumbs=True)
            with open(CACHE_FILE,'w') as fh: json.dump(files,fh)
            if first_run:
                print(f'Initial scan complete: {len(files)} files cached')
                first_run=False
        except Exception as e: print('BG error:',e)
        time.sleep(60)

threading.Thread(target=background_processor,daemon=True).start()

# -----------------------------------------------------------------------
# Pass schedule
# -----------------------------------------------------------------------
def get_passes():
    now = datetime.datetime.now(datetime.timezone.utc)
    epoch = time.time()
    minute = now.minute
    passes = {}

    def in_window(sat_key):
        for start,end in SAT_WINDOWS.get(sat_key,[]):
            if start<=minute<end: return True
        return False

    def mins_left(sat_key):
        for start,end in SAT_WINDOWS.get(sat_key,[]):
            if start<=minute<end: return end-minute
        return 0

    def current_window_end_ts(sat_key):
        """Return epoch ts of end of current window if active, else None."""
        for start,end in SAT_WINDOWS.get(sat_key,[]):
            if start<=minute<end:
                t=now.replace(minute=end,second=0,microsecond=0)
                return epoch+(t-now).total_seconds()
        return None

    def next_window_ts(sat_key):
        windows = SAT_WINDOWS.get(sat_key,[(0,30)])
        for h_offset in range(24):
            base=(now+datetime.timedelta(hours=h_offset)).replace(minute=0,second=0,microsecond=0)
            for start,_ in windows:
                t=base.replace(minute=start)
                if t>now: return epoch+(t-now).total_seconds()
        return epoch+3600

    for sat_key,label,interval in [
        ("fy_svissr","FengYun-2H","Hourly"),
        ("l3_hrit","Elektro-L3","Every 3h"),
        ("l5_hrit","Elektro-L5","Every 30m"),
    ]:
        active = in_window(sat_key)
        ml = mins_left(sat_key)
        # When receiving: show time remaining until end of window
        # When not receiving: show time until next window starts
        if active:
            ts = current_window_end_ts(sat_key) or epoch
        else:
            ts = next_window_ts(sat_key)
        passes[sat_key] = {
            "label":label,"interval":interval,
            "ts":ts,
            "active":active,
            "mins_left":ml,
            "is_current":ACTIVE_SAT==sat_key
        }
    return passes

# -----------------------------------------------------------------------
# Page routes
# -----------------------------------------------------------------------
@app.after_request
def add_security_headers(resp):
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'SAMEORIGIN'
    resp.headers['Referrer-Policy'] = 'no-referrer'
    resp.headers.pop('Server', None)
    return resp

@app.route('/')
@app.route('/library')
@app.route('/calendar')
@app.route('/comments-feed')
@app.route('/favorites')
@app.route('/profile')
@app.route('/profile/<username>')
@app.route('/messages')
def index(**_): return render_template('index.html', contact_email=CONTACT_EMAIL)

@app.route('/favicon.ico')
def favicon():
    fp = os.path.join(app.static_folder,'favicon.png')
    return send_file(fp,mimetype='image/png') if os.path.exists(fp) else ('',204)

# -----------------------------------------------------------------------
# Auth API
# -----------------------------------------------------------------------
@app.route('/api/auth/register', methods=['POST'])
def register():
    return jsonify({'status':'error','message':'Registration disabled.'}),403

@app.route('/api/auth/login', methods=['POST'])
def login():
    d=request.json or {}
    login_val=(d.get('username') or d.get('email') or '').strip()
    pw=d.get('password','')
    with get_db() as c:
        row=c.execute('SELECT * FROM users WHERE username=? OR email=?',(login_val,login_val.lower())).fetchone()
    if not row or not verify_pw(pw,row['password_hash'],row['salt']):
        return jsonify({"status":"error","message":"Invalid username or password."}),401
    token,_=create_session(row['id'])
    resp=make_response(jsonify({"status":"ok","user":user_dict(row)}))
    resp.set_cookie('st',token,max_age=SESSION_DAYS*86400,httponly=True,samesite='Lax')
    return resp

@app.route('/api/auth/logout', methods=['POST'])
def logout():
    token=request.cookies.get('st')
    if token:
        with get_db() as c: c.execute('DELETE FROM sessions WHERE token=?',(token,))
    resp=make_response(jsonify({"status":"ok"}))
    resp.delete_cookie('st')
    return resp

@app.route('/api/auth/me')
def me():
    u=get_user()
    if not u: return jsonify({"user":None})
    with get_db() as c:
        unread_n=c.execute('SELECT COUNT(*) FROM notifications WHERE user_id=? AND read=0',(u['id'],)).fetchone()[0]
        unread_m=c.execute('SELECT COUNT(*) FROM messages WHERE to_user_id=? AND read=0',(u['id'],)).fetchone()[0]
    return jsonify({"user":user_dict(u),"unread_notifications":unread_n,"unread_messages":unread_m})

@app.route('/api/auth/profile', methods=['PATCH'])
def update_profile():
    u,err=require_user()
    if err: return err
    d=request.json or {}
    updates={}
    if d.get('username','').strip(): updates['username']=d['username'].strip()[:50]
    if 'email_notify' in d: updates['email_notify']=1 if d['email_notify'] else 0
    if d.get('new_password') and len(d['new_password'])>=6:
        with get_db() as c:
            row=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
        if not verify_pw(d.get('old_password',''),row['password_hash'],row['salt']):
            return jsonify({"status":"error","message":"Incorrect current password."}),403
        h,s=hash_pw(d['new_password']); updates['password_hash']=h; updates['salt']=s
    if updates:
        sc=', '.join('%s=?'%k for k in updates)
        with get_db() as c: c.execute('UPDATE users SET %s WHERE id=?'%sc,(*updates.values(),u['id']))
    with get_db() as c:
        row=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
    return jsonify({"status":"ok","user":user_dict(row)})

@app.route('/api/auth/avatar', methods=['POST'])
def upload_avatar():
    u,err=require_user()
    if err: return err
    f=request.files.get('avatar')
    if not f: return jsonify({"status":"error","message":"No file."}),400
    ext=f.filename.rsplit('.',1)[-1].lower() if '.' in f.filename else ''
    if ext not in ('png','jpg','jpeg','gif','webp'): return jsonify({"status":"error","message":"Invalid type."}),400
    fname='avatar_%d.%s'%(u['id'],ext)
    f.save(os.path.join(AVATAR_DIR,fname))
    with get_db() as c: c.execute('UPDATE users SET avatar=? WHERE id=?',(fname,u['id']))
    return jsonify({"status":"ok","avatar":fname})

@app.route('/api/users/<username>')
def get_user_profile(username):
    with get_db() as c:
        u=c.execute('SELECT id,username,avatar,is_owner,created_ts FROM users WHERE username=?',(username,)).fetchone()
        if not u: return jsonify({"status":"error","message":"User not found."}),404
        uid=u['id']
        favs=c.execute('SELECT image_name FROM favorites WHERE user_id=? ORDER BY ts DESC LIMIT 50',(uid,)).fetchall()
        cmts=c.execute('SELECT * FROM comments WHERE user_id=? ORDER BY ts DESC LIMIT 50',(uid,)).fetchall()
    return jsonify({
        "user":{"id":uid,"username":u['username'],"avatar":u['avatar'],"is_owner":bool(u['is_owner']),"created_ts":u['created_ts']},
        "favorites":[f['image_name'] for f in favs],
        "comments":[dict(c) for c in cmts]
    })

# -----------------------------------------------------------------------
# Notifications
# -----------------------------------------------------------------------
@app.route('/api/notifications')
def get_notifications():
    u,err=require_user()
    if err: return err
    with get_db() as c:
        rows=c.execute('SELECT * FROM notifications WHERE user_id=? ORDER BY ts DESC LIMIT 50',(u['id'],)).fetchall()
        c.execute('UPDATE notifications SET read=1 WHERE user_id=?',(u['id'],))
    return jsonify([dict(r) for r in rows])

# -----------------------------------------------------------------------
# Messages
# -----------------------------------------------------------------------
@app.route('/api/messages')
def get_messages():
    u,err=require_user()
    if err: return err
    with_id=request.args.get('with',0,type=int)
    with get_db() as c:
        if with_id:
            rows=c.execute(
                'SELECT m.*,uf.username as from_name,uf.avatar as from_avatar '
                'FROM messages m JOIN users uf ON m.from_user_id=uf.id '
                'WHERE (m.from_user_id=? AND m.to_user_id=?) OR (m.from_user_id=? AND m.to_user_id=?) '
                'ORDER BY m.ts ASC',(u['id'],with_id,with_id,u['id'])
            ).fetchall()
            c.execute('UPDATE messages SET read=1 WHERE to_user_id=? AND from_user_id=?',(u['id'],with_id))
        else:
            rows=c.execute(
                'SELECT m.*,uf.username as from_name,uf.avatar as from_avatar '
                'FROM messages m JOIN users uf ON m.from_user_id=uf.id '
                'WHERE m.from_user_id=? OR m.to_user_id=? ORDER BY m.ts DESC',(u['id'],u['id'])
            ).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/messages', methods=['POST'])
def send_message():
    u,err=require_user()
    if err: return err
    d=request.json or {}
    to_id=d.get('to_user_id')
    text=(d.get('text') or '').strip()[:2000]
    if not to_id or not text: return jsonify({"status":"error","message":"Missing fields."}),400
    with get_db() as c:
        to_user=c.execute('SELECT id,username FROM users WHERE id=?',(to_id,)).fetchone()
        if not to_user: return jsonify({"status":"error","message":"User not found."}),404
        cur=c.execute('INSERT INTO messages (from_user_id,to_user_id,text,ts) VALUES (?,?,?,?)',(u['id'],to_id,text,time.time()))
        c.execute('INSERT INTO notifications (user_id,type,message,link,ts) VALUES (?,?,?,?,?)',
                  (to_id,'message','New message from %s'%u['username'],'/messages',time.time()))
        to_row=c.execute('SELECT email,username,email_notify FROM users WHERE id=?',(to_id,)).fetchone()
    if to_row and to_row['email_notify']:
        html=('<p>Hi %s,</p><p><b>%s</b> sent you a message:</p>'
              '<blockquote style="border-left:3px solid #4facfe;padding:10px;margin:10px 0">%s</blockquote>'
              '<p><a href="http://localhost:8080/messages" style="background:#4facfe;color:#fff;padding:10px 20px;border-radius:4px;text-decoration:none">View Message</a></p>'
              %(to_row['username'],u['username'],text[:300]))
        send_email(to_row['email'],'New message received',html)
    return jsonify({"status":"ok","message_id":cur.lastrowid})

@app.route('/api/messages/users')
def get_message_users():
    u,err=require_user()
    if err: return err
    with get_db() as c:
        rows=c.execute(
            'SELECT CASE WHEN m.from_user_id=? THEN m.to_user_id ELSE m.from_user_id END as other_id,'
            'uu.username,uu.avatar,MAX(m.ts) as last_ts,'
            'SUM(CASE WHEN m.to_user_id=? AND m.read=0 THEN 1 ELSE 0 END) as unread '
            'FROM messages m JOIN users uu ON uu.id=(CASE WHEN m.from_user_id=? THEN m.to_user_id ELSE m.from_user_id END) '
            'WHERE m.from_user_id=? OR m.to_user_id=? GROUP BY other_id ORDER BY last_ts DESC',
            (u['id'],u['id'],u['id'],u['id'],u['id'])
        ).fetchall()
    return jsonify([dict(r) for r in rows])

# -----------------------------------------------------------------------
# Mail config
# -----------------------------------------------------------------------
@app.route('/api/mail-config', methods=['GET','POST'])
def mail_config():
    u=get_user()
    if not u or not u['is_owner']: return jsonify({"status":"error"}),403
    if request.method=='GET':
        with get_db() as c:
            row=c.execute('SELECT smtp_email,enabled FROM mail_config WHERE id=1').fetchone()
        return jsonify(dict(row) if row else {})
    d=request.json or {}
    with get_db() as c:
        c.execute('UPDATE mail_config SET smtp_email=?,smtp_password=?,enabled=? WHERE id=1',
                  (d.get('smtp_email',''),d.get('smtp_password',''),1 if d.get('enabled') else 0))
    return jsonify({"status":"ok"})


@app.route('/api/data')
def get_data():
    total,used,_=shutil.disk_usage(BASE_DATA_DIR)
    temp=_pi_cache.get('temp',0)
    pi_disk=_pi_cache.get('disk',{"percent":0,"used_gb":0,"total_gb":0})
    tasks=_pi_cache.get('tasks',{"sync":False,"align":False,"capture":False})
    total_captures=0
    try:
        with open(CACHE_FILE) as fh: total_captures=len(json.load(fh))
    except Exception: pass
    with get_db() as c:
        total_comments=c.execute('SELECT COUNT(*) FROM comments').fetchone()[0]
        total_favs=c.execute('SELECT COUNT(*) FROM favorites').fetchone()[0]
        peak_row=c.execute('SELECT MAX(temp) FROM peak_temps WHERE ts>?',(time.time()-86400,)).fetchone()
        peak_temp=peak_row[0] if peak_row and peak_row[0] else temp
    img_size_gb=_img_size_cache
    return jsonify({
        "system":{"temp":temp,"peak_temp":peak_temp,
                  "disk_nas":{"percent":round(used/total*100,1) if total else 0,"used_gb":round(used/2**30,1),"total_gb":round(total/2**30,1),"img_gb":img_size_gb},
                  "disk_sd":pi_disk,"pi_online":_pi_cache.get("online",False),"pi_last_seen":_pi_cache.get("last_seen",0)},
        "passes":get_passes(),"tasks":tasks,
        "stats":{"captures":total_captures,"comments":total_comments,"favorites":total_favs}
    })

@app.route('/api/files')
def get_files():
    try:
        with open(CACHE_FILE) as fh:
            data = fh.read()
        # ETag based on content length+mtime for browser caching
        import hashlib
        etag = hashlib.md5(data.encode()).hexdigest()[:16]
        if_none_match = request.headers.get('If-None-Match','')
        if if_none_match == etag:
            return make_response('', 304)
        resp = make_response(data, 200)
        resp.headers['Content-Type'] = 'application/json'
        resp.headers['ETag'] = etag
        resp.headers['Cache-Control'] = 'no-cache'  # revalidate but use cache if unchanged
        return resp
    except Exception: return jsonify([])

@app.route('/api/control', methods=['POST'])
def control():
    d=request.json or {}
    u=get_user()
    is_owner=u and u['is_owner']
    if not is_owner:
        return jsonify({"status":"error","message":"Owner only."}),403
    if d.get('action')=='delete':
        rel=d.get('filename','')
        for p in (os.path.join(CLOUD_VIEW,rel),thumb_path(rel)):
            if os.path.exists(p): os.remove(p)
        return jsonify({"status":"ok"})
    # Always send the PIN to the Pi in case its API requires it
    payload = dict(d)
    payload['pin'] = ADMIN_PIN
    try:
        r=requests.post('%s/control'%PI_API,json=payload,timeout=5)
        return jsonify(r.json()),r.status_code
    except Exception:
        return jsonify({"status":"error","message":"Pi offline."}),500

@app.route('/api/logs/<session_name>')
def get_logs(session_name):
    try:
        r=requests.get('%s/logs/%s'%(PI_API,session_name),timeout=5)
        return jsonify(r.json())
    except Exception:
        return jsonify({"log":"Pi API offline."})

# -----------------------------------------------------------------------
# Captcha
# -----------------------------------------------------------------------
@app.route('/api/captcha')
def captcha():
    token,q=new_captcha()
    return jsonify({"token":token,"question":q})

# -----------------------------------------------------------------------
# Comments
# -----------------------------------------------------------------------
@app.route('/api/comments')
def get_comments():
    image=request.args.get('image')
    sort=request.args.get('sort','recent')
    order=request.args.get('order','desc')
    u=get_user(); uid=u['id'] if u else 0
    try:
        with get_db() as c:
            order_dir='ASC' if order=='asc' else 'DESC'
            sort_col='like_count' if sort=='liked' else 'cm.ts'
            q=('SELECT cm.id,cm.image_name,cm.user_id,cm.author,cm.text,cm.parent_id,cm.ts,'
               'u.avatar,u.is_owner,'
               'COUNT(DISTINCT cl.id) as like_count,'
               'MAX(CASE WHEN cl.user_id=? THEN 1 ELSE 0 END) as user_liked '
               'FROM comments cm LEFT JOIN users u ON cm.user_id=u.id '
               'LEFT JOIN comment_likes cl ON cl.comment_id=cm.id ')
            if image:
                rows=c.execute(q+'WHERE cm.image_name=? GROUP BY cm.id ORDER BY %s %s LIMIT 200'%(sort_col,order_dir),(uid,image)).fetchall()
            else:
                rows=c.execute(q+'GROUP BY cm.id ORDER BY %s %s LIMIT 100'%(sort_col,order_dir),(uid,)).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        print('get_comments error:',e)
        return jsonify([])

@app.route('/api/comments', methods=['POST'])
def post_comment():
    d=request.json or {}
    text=clean((d.get('text') or '').strip()[:500])
    image=(d.get('image_name') or '').strip()
    parent_id=d.get('parent_id')
    if not text or not image: return jsonify({"status":"error","message":"Missing fields."}),400
    # Use nickname - admin posts as Owner, others use provided nickname
    u=get_user()
    if u and u.get('is_owner'):
        author='Owner'
    else:
        raw_nick=clean((d.get('nickname') or 'Anonymous').strip()[:40]) or 'Anonymous'
        author='Anonymous' if raw_nick.lower()=='owner' else raw_nick
    is_owner=1 if (u and u.get('is_owner')) else 0
    with get_db() as c:
        cur=c.execute('INSERT INTO comments (image_name,user_id,author,text,parent_id,ts,is_owner) VALUES (?,?,?,?,?,?,?)',
                      (image,0,author,text,parent_id,int(time.time()),is_owner))
        cid=cur.lastrowid
        if d.get('cap_token') and d.get('cap_ans'):
            pass  # captcha already verified client-side
    return jsonify({"status":"ok","id":cid,"author":author,"is_owner":is_owner})


@app.route('/api/comments/<int:cid>', methods=['DELETE'])
def del_comment(cid):
    u=get_user()
    with get_db() as c:
        row=c.execute('SELECT * FROM comments WHERE id=?',(cid,)).fetchone()
        if not row: return jsonify({"status":"error","message":"Not found."}),404
        if not u or not(u['is_owner'] or u['id']==row['user_id']):
            return jsonify({"status":"error","message":"Not authorized."}),403
        c.execute('DELETE FROM comments WHERE id=?',(cid,))
    return jsonify({"status":"ok"})

@app.route('/api/comments/<int:cid>/like', methods=['POST'])
def toggle_comment_like(cid):
    u,err=require_user()
    if err: return err
    with get_db() as c:
        ex=c.execute('SELECT id FROM comment_likes WHERE comment_id=? AND user_id=?',(cid,u['id'])).fetchone()
        if ex:
            c.execute('DELETE FROM comment_likes WHERE id=?',(ex['id'],)); action='removed'
        else:
            c.execute('INSERT INTO comment_likes (comment_id,user_id,ts) VALUES (?,?,?)',(cid,u['id'],time.time())); action='added'
        count=c.execute('SELECT COUNT(*) FROM comment_likes WHERE comment_id=?',(cid,)).fetchone()[0]
    return jsonify({"status":"ok","action":action,"count":count,"liked":action=='added'})

# -----------------------------------------------------------------------
# Favorites
# -----------------------------------------------------------------------
@app.route('/api/favorites')
def get_favs():
    image=request.args.get('image','')
    with get_db() as c:
        if image:
            faved=bool(c.execute('SELECT 1 FROM favorites WHERE image_name=? LIMIT 1',(image,)).fetchone())
            return jsonify({"favorited":faved})
        rows=c.execute('SELECT DISTINCT image_name FROM favorites').fetchall()
    return jsonify([{"image_name":r['image_name']} for r in rows])


@app.route('/api/favorites', methods=['POST'])
def toggle_fav():
    d=request.json or {}
    image=(d.get('image_name') or '').strip()
    if not image: return jsonify({"status":"error","message":"Missing image."}),400
    action=d.get('action','add')
    u=get_user()
    with get_db() as c:
        if action=='clear':
            if not (u and u.get('is_owner')): return jsonify({"status":"error","message":"Admin only."}),403
            c.execute('DELETE FROM favorites WHERE image_name=?',(image,))
            return jsonify({"status":"ok","favorited":False})
        # add: one-way, INSERT OR IGNORE so first click sticks
        c.execute('INSERT OR IGNORE INTO favorites (image_name,user_id,ts) VALUES (?,0,?)',(image,__import__('time').time()))
        faved=bool(c.execute('SELECT 1 FROM favorites WHERE image_name=? LIMIT 1',(image,)).fetchone())
    return jsonify({"status":"ok","favorited":faved})


@app.route('/api/favorites/clear', methods=['POST'])
def clear_favs():
    u=get_user()
    if not u or not u['is_owner']: return jsonify({"status":"error"}),403
    d=request.json or {}
    image=d.get('image_name')
    with get_db() as c:
        if image: c.execute('DELETE FROM favorites WHERE image_name=?',(image,))
        else: c.execute('DELETE FROM favorites')
    return jsonify({"status":"ok"})

# -----------------------------------------------------------------------
# Announcements
# -----------------------------------------------------------------------
@app.route('/api/announcements')
def get_anns():
    with get_db() as c:
        rows=c.execute('SELECT * FROM announcements ORDER BY ts DESC').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/announcements', methods=['POST'])
def post_ann():
    u=get_user()
    if not u or not u['is_owner']: return jsonify({"status":"error"}),403
    text=(request.json or {}).get('text','').strip()
    if not text: return jsonify({"status":"error","message":"Empty."}),400
    with get_db() as c:
        cur=c.execute('INSERT INTO announcements (text,ts) VALUES (?,?)',(text,time.time()))
        row=c.execute('SELECT * FROM announcements WHERE id=?',(cur.lastrowid,)).fetchone()
    return jsonify({"status":"ok","announcement":dict(row)})

@app.route('/api/announcements/<int:aid>', methods=['DELETE'])
def del_ann(aid):
    u=get_user()
    if not u or not u['is_owner']: return jsonify({"status":"error"}),403
    with get_db() as c: c.execute('DELETE FROM announcements WHERE id=?',(aid,))
    return jsonify({"status":"ok"})

@app.route('/api/sat-configs', methods=['GET'])
def get_sat_configs():
    with get_db() as c:
        rows=c.execute('SELECT * FROM satellite_configs ORDER BY id').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/sat-configs', methods=['POST'])
def save_sat_config():
    u=get_user()
    if not u or not u['is_owner']: return jsonify({"status":"error"}),403
    d=request.json or {}
    sat_key=(d.get('sat_key') or '').strip()
    name=(d.get('name') or '').strip()
    pattern=(d.get('file_pattern') or '').strip()
    if not sat_key or not name or not pattern: return jsonify({"status":"error","message":"Missing fields."}),400
    with get_db() as c:
        c.execute('INSERT OR REPLACE INTO satellite_configs (sat_key,name,file_pattern,active) VALUES (?,?,?,1)',(sat_key,name,pattern))
    return jsonify({"status":"ok"})

@app.route('/api/sat-configs/<sat_key>', methods=['DELETE'])
def del_sat_config(sat_key):
    u=get_user()
    if not u or not u['is_owner']: return jsonify({"status":"error"}),403
    with get_db() as c:
        c.execute('DELETE FROM satellite_configs WHERE sat_key=?',(sat_key,))
        c.execute('DELETE FROM channel_configs WHERE sat_key=?',(sat_key,))
    return jsonify({"status":"ok"})

@app.route('/api/chan-configs', methods=['GET'])
def get_chan_configs():
    sat=request.args.get('sat','')
    with get_db() as c:
        if sat:
            rows=c.execute('SELECT * FROM channel_configs WHERE sat_key=? ORDER BY sort_order,id',(sat,)).fetchall()
        else:
            rows=c.execute('SELECT * FROM channel_configs ORDER BY sat_key,sort_order,id').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/chan-configs', methods=['POST'])
def save_chan_config():
    u=get_user()
    if not u or not u['is_owner']: return jsonify({"status":"error"}),403
    d=request.json or {}
    sat_key=(d.get('sat_key') or '').strip()
    chan_key=(d.get('chan_key') or '').strip()
    name=(d.get('name') or '').strip()
    pattern=(d.get('file_pattern') or '').strip()
    if not sat_key or not chan_key or not name or not pattern:
        return jsonify({"status":"error","message":"Missing required fields."}),400
    with get_db() as c:
        c.execute('''INSERT INTO channel_configs (sat_key,chan_key,name,description,wavelength,file_pattern,example_image,delete_threshold_mb,sort_order)
                     VALUES (?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(sat_key,chan_key) DO UPDATE SET
                       name=excluded.name,description=excluded.description,wavelength=excluded.wavelength,
                       file_pattern=excluded.file_pattern,example_image=excluded.example_image,
                       delete_threshold_mb=excluded.delete_threshold_mb,sort_order=excluded.sort_order''',
                  (sat_key,chan_key,name,d.get('description',''),d.get('wavelength',''),
                   pattern,d.get('example_image'),d.get('delete_threshold_mb',0),d.get('sort_order',99)))
    return jsonify({"status":"ok"})

@app.route('/api/chan-configs/<int:cid>', methods=['DELETE'])
def del_chan_config(cid):
    u=get_user()
    if not u or not u['is_owner']: return jsonify({"status":"error"}),403
    with get_db() as c: c.execute('DELETE FROM channel_configs WHERE id=?',(cid,))
    return jsonify({"status":"ok"})

@app.route('/api/chan-configs/<int:cid>/example', methods=['POST'])
def set_chan_example(cid):
    u=get_user()
    if not u or not u['is_owner']: return jsonify({"status":"error"}),403
    img=(request.json or {}).get('image_name','')
    with get_db() as c: c.execute('UPDATE channel_configs SET example_image=? WHERE id=?',(img,cid))
    return jsonify({"status":"ok"})

@app.route('/api/chan-configs/sizes')
def chan_sizes():
    """Return last 5 file sizes for each channel pattern."""
    u=get_user()
    if not u or not u['is_owner']: return jsonify({"status":"error"}),403
    try:
        with open(CACHE_FILE) as fh: files=json.load(fh)
    except Exception: return jsonify({})
    with get_db() as c:
        chans=c.execute('SELECT id,sat_key,chan_key,file_pattern FROM channel_configs').fetchall()
    result={}
    for ch in chans:
        key='%s:%s'%(ch['sat_key'],ch['chan_key'])
        pat=ch['file_pattern']
        matches=[f for f in files if pat in f['name']][:5]
        result[key]=[round(f['size_mb'],2) for f in matches]
    return jsonify(result)

@app.route('/api/unknown-files')
def get_unknown_files():
    u=get_user()
    if not u or not u['is_owner']: return jsonify([])
    with get_db() as c:
        rows=c.execute('SELECT * FROM unknown_files WHERE resolved=0 ORDER BY first_seen DESC LIMIT 50').fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/unknown-files/<int:fid>/resolve', methods=['POST'])
def resolve_unknown(fid):
    u=get_user()
    if not u or not u['is_owner']: return jsonify({"status":"error"}),403
    d=request.json or {}
    action=d.get('action','ignore')
    with get_db() as c:
        row=c.execute('SELECT filename FROM unknown_files WHERE id=?',(fid,)).fetchone()
        if not row: return jsonify({"status":"error","message":"Not found."}),404
        if action=='delete':
            fp=os.path.join(CLOUD_VIEW,row['filename'])
            if os.path.exists(fp): os.remove(fp)
        c.execute('UPDATE unknown_files SET resolved=1 WHERE id=?',(fid,))
    return jsonify({"status":"ok"})

@app.route('/api/users')
def list_users():
    u=get_user()
    if not u: return jsonify([])
    with get_db() as c:
        rows=c.execute('SELECT id,username,avatar FROM users WHERE id!=? ORDER BY username LIMIT 100',(u['id'],)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/users/search')
def search_users():
    q=(request.args.get('q') or '').strip()
    if len(q)<2: return jsonify([])
    with get_db() as c:
        rows=c.execute('SELECT id,username,avatar FROM users WHERE username LIKE ? LIMIT 10',('%'+q+'%',)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route('/api/auth/delete', methods=['POST'])
def delete_account():
    u,err=require_user()
    if err: return err
    d=request.json or {}
    pw=d.get('password','')
    with get_db() as c:
        row=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
        if not verify_pw(pw,row['password_hash'],row['salt']):
            return jsonify({"status":"error","message":"Incorrect password."}),403
        if row['is_owner']:
            return jsonify({"status":"error","message":"The owner account cannot be deleted."}),403
        c.execute('DELETE FROM users WHERE id=?',(u['id'],))
    resp=make_response(jsonify({"status":"ok"}))
    resp.delete_cookie('st')
    return resp


@app.route('/api/messages/conversation/<int:other_id>', methods=['DELETE'])
def delete_conversation(other_id):
    u,err=require_user()
    if err: return err
    with get_db() as c:
        c.execute('DELETE FROM messages WHERE (from_user_id=? AND to_user_id=?) OR (from_user_id=? AND to_user_id=?)',
                  (u['id'],other_id,other_id,u['id']))
    return jsonify({"status":"ok"})


@app.route('/api/channel-previews')
def channel_previews():
    sat_filter=request.args.get('sat','')
    try:
        with open(CACHE_FILE) as fh: files=json.load(fh)
    except Exception: return jsonify({})
    with get_db() as c:
        if sat_filter:
            chans=c.execute('SELECT * FROM channel_configs WHERE sat_key=? ORDER BY sort_order',(sat_filter,)).fetchall()
        else:
            chans=c.execute('SELECT * FROM channel_configs ORDER BY sat_key,sort_order').fetchall()
    result={}
    for ch in chans:
        key=ch['chan_key']
        # If owner set an example image, use that
        if ch['example_image']:
            result[key]={"name":ch['chan_key'],"label":ch['name'],"description":ch['description'],"wavelength":ch['wavelength'],"example":ch['example_image'],"id":ch['id']}
            continue
        # Otherwise find most recent matching file
        pat=ch['file_pattern']
        matches=[f for f in files if pat in f['name']]
        if sat_filter:
            sat_row=None
            with get_db() as c2:
                sat_row=c2.execute('SELECT file_pattern FROM satellite_configs WHERE sat_key=?',(sat_filter,)).fetchone()
            if sat_row:
                matches=[f for f in matches if sat_row['file_pattern'] in f['name']]
        example=matches[0]['name'] if matches else None
        result[key]={"name":ch['chan_key'],"label":ch['name'],"description":ch['description'],"wavelength":ch['wavelength'],"example":example,"id":ch['id']}
    return jsonify(result)

# -----------------------------------------------------------------------
# Static
# -----------------------------------------------------------------------

@app.route('/api/images/delete', methods=['POST'])
def delete_image():
    u=get_user()
    if not u or not u['is_owner']: return jsonify({"status":"error","message":"Owner only."}),403
    d=request.json or {}
    rel=d.get('filename','').strip()
    if not rel: return jsonify({"status":"error","message":"Missing filename."}),400
    deleted=[]
    for p in (os.path.join(CLOUD_VIEW,rel), thumb_path(rel)):
        if os.path.exists(p):
            try: os.remove(p); deleted.append(p)
            except Exception as e: print('delete error:',e)
    # Remove from favorites and comments
    with get_db() as c:
        c.execute('DELETE FROM favorites WHERE image_name=?',(rel,))
        c.execute('DELETE FROM comments WHERE image_name=?',(rel,))
    return jsonify({"status":"ok","deleted":len(deleted)})

@app.route('/thumb/<path:filename>')
def serve_thumb(filename):
    tp=thumb_path(filename)
    if os.path.exists(tp): return send_file(tp,mimetype='image/jpeg')
    op=os.path.join(CLOUD_VIEW,filename)
    if not os.path.exists(op): return "Not found",404
    with thumb_lock:
        if os.path.exists(tp): return send_file(tp,mimetype='image/jpeg')
        try:
            with Image.open(op) as img:
                img=make_jpeg_safe(img); img.thumbnail((1200,1200)); img.save(tp,'JPEG',quality=85)
            return send_file(tp,mimetype='image/jpeg')
        except Exception: return send_from_directory(CLOUD_VIEW,filename)

@app.route('/images/<path:filename>')
def serve_image(filename): return send_from_directory(CLOUD_VIEW,filename)

@app.route('/avatars/<path:filename>')
def serve_avatar(filename): return send_from_directory(AVATAR_DIR,filename)

@app.route('/api/comments/<int:cid>/like', methods=['POST'])
def like_comment(cid):
    d=request.json or {}
    action=d.get('action','add')  # 'add' or 'remove'
    with get_db() as c:
        if action=='add':
            try: c.execute('INSERT INTO comment_likes (comment_id,user_id) VALUES (?,0)',(cid,))
            except Exception: pass
        elif action=='remove':
            c.execute('DELETE FROM comment_likes WHERE comment_id=? AND id=(SELECT id FROM comment_likes WHERE comment_id=? LIMIT 1)',(cid,cid))
        count=c.execute('SELECT COUNT(*) FROM comment_likes WHERE comment_id=?',(cid,)).fetchone()[0]
    return jsonify({"liked":action=='add',"count":count})


if __name__ == '__main__':
    app.run(host='0.0.0.0',port=8080,debug=False,use_reloader=False)
