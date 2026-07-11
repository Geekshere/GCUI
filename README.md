# GCUI - Ground Control User Interface

A Flask-based web dashboard for a homebrew weather satellite ground station.
GCUI shows the live status of a Raspberry Pi based SDR capture rig, browses
and serves captured satellite imagery (with thumbnails), and adds a small
community layer on top: comments, likes, favorites, announcements, etc.

This was built for my own weather satellite ground station setup (RTL-SDR
on a Raspberry Pi, receiving imagery off my roof). It took a good amount of
time to get right, and honestly went through a lot of different versions
along the way as I figured out what I actually wanted the site to do. This
repo is the latest version of that process, not a first draft.

## Why this exists (and why it's a little rough in places)

I built this by iterating with AI, shaping it to fit exactly what I wanted
for my own station's site. What I wanted kept changing as I went, so the
code reflects a project that evolved a lot rather than one planned out from
the start.

I'm publishing it as is because I think it's a genuinely useful starting
point for anyone else running a ground station who wants a web front end
for it. I would not recommend deploying it unmodified though. Every ground
station setup is different: different hardware, different satellites,
different hosting. Fork it and use an AI coding assistant to reshape it
around your own setup and your own taste. That is basically how it got made
in the first place.

## What it does

- Live status panel for the Pi (disk usage, capture state) via `/api/data`
- Browses captured images with auto-generated thumbnails
- Per-satellite and per-channel config, so you can label passes and
  channels the way your capture pipeline names them
- Accounts, comments (with a captcha for anonymous posters and a basic
  profanity filter), likes, and favorites on images
- Announcements feed and simple user-to-user messaging
- Optional email notifications (via SMTP) when someone replies to your
  comment
- Owner-only admin controls, gated behind an account flag and a PIN prompt
  in the UI

## Stack

- Python / Flask
- SQLite (file-based, no separate DB server needed)
- Vanilla HTML/CSS/JS front end (single template, no build step)
- Docker / Docker Compose for deployment

## Setup

### 1. Configure your environment

Copy the example env file and fill in your own values:

```bash
cp .env.example .env
```

Edit `.env`:

| Variable | What it's for |
|---|---|
| `OWNER_EMAIL`, `OWNER_USERNAME`, `OWNER_PASSWORD` | The admin/owner account, seeded into the database the first time the app starts. Change the password before you ever deploy this publicly. |
| `ADMIN_PIN` | A 4-digit PIN the UI asks for on certain owner actions, as a second confirmation step. |
| `PI_IP`, `PI_USER` | The address and user of the machine running your actual SDR capture, if you want GCUI polling it for status. |

`.env` is gitignored, so never commit it. If you ever accidentally commit
real credentials, rotate them immediately. Removing the file in a later
commit does not remove it from git history.

### 2. Point it at your data directory

By default `compose.yaml` maps `./data` on the host to `/app/data` in the
container (images, thumbnails, avatars, and the SQLite DB all live there).
Change that volume mapping to wherever you want this stored, whether that's
a NAS share, an external drive, or something else.

### 3. Run it

```bash
docker compose up -d --build
```

The app will be available on port `8080`. Put it behind a reverse proxy
(Caddy, nginx, Cloudflare Tunnel, whatever you already use) if you want it
reachable outside your LAN.

### 4. Log in

On first run, the owner account is created from the `OWNER_*` env vars
above. Log in with those credentials to access admin features.

## A couple of things worth knowing before you deploy

- The admin PIN and owner password are the only things standing between the
  public internet and your ground station's admin panel if you expose this
  directly. Put it behind a reverse proxy with TLS, and don't reuse a
  password from anywhere else.
- This was written for a single-operator hobby project, not audited for
  multi-tenant or high-traffic use. Treat it accordingly.

## License

GPLv3, same as my other public projects. Do whatever you want with it, just
keep it open.
