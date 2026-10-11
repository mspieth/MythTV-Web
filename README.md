# MythTV Web Server

A Flask application intended to provide a web interface to the MythTV services on this host. Apache/Nginx/other is expected to handle external access, then reverse-proxy requests to this application.

It provides program selection, recording setup, and a web player that provides all of the features that MythFrontend provides, including playing a program currently recording, and skipping, and timestretch, all from a browser.

It is mobile browser compatible and Android TV with TVBro.

This started out as a fun project to play with AI and is not meant to replace the embedded browser found in MythTV itself.

## Quick start

Install [uv using its official installation guide](https://docs.astral.sh/uv/getting-started/installation/) and Node.js 20 or newer, then run these commands from the project directory:

```sh
npm ci
npm run build:browser
uv run mythtv-web
```

Open `http://127.0.0.1:6555/` in your browser. On first startup, sign in as `admin` with password `mythtv-admin`, then change the password when prompted. Stop the server with Ctrl+C.

`uv run` creates the project's `.venv/` and installs Python dependencies automatically. Application dependencies and development tools are declared in `pyproject.toml`; `uv.lock` records the resolved versions. Rebuild browser assets after checkout or browser dependency updates. See [Run](#run) and [Background service](#background-service) for other ways to start the server.

The build currently packages FFmpeg.wasm assets, but browser-side transcoding is disabled; the player does not load or use them. See the TODO under Browser playback.

## Recordings library

The home page groups recordings by program title, like MythFrontend. Selecting a group shows all recordings in that group. Flask loads the full recorded-program list from MythTV's `Dvr/GetRecordedList` service and caches it under `.run/` for two minutes so every Gunicorn worker can serve complete groups consistently. The first group load can take several seconds for a large library. Flask also proxies cover art locally, so the browser does not connect to MythTV directly. The default backend URL is `http://127.0.0.1:6544`; set `MYTHTV_BACKEND_URL` if the local backend uses another address. Search and watched-state filters are available in both views.

Each worker keeps a parsed copy of the recordings cache and checks the file's inode, modification time, and size before reusing it. Fresh reads avoid the shared refresh lock; deletion invalidates the disk cache so all workers notice the change on their next lookup.

## Videos library

The Videos tab lists MythTV video-library entries from `Video/GetVideoList`, with search, watched-state filters, artwork, and browser playback. If artwork is missing or its file is unavailable, Flask generates and caches a poster frame from `Content/GetVideo`; this fallback requires system `ffmpeg`. Video streams are served through Flask, and watched status updates use `Video/UpdateVideoWatchedStatus`. MythTV service URLs remain server-side.

## Guide and backend status

The Upcoming tab shows scheduled programs, while Guide browses a four-hour program window and can create, edit, or delete recording rules through MythTV's `Dvr` services. These actions require a Flask account session, but Flask does not pass account identity to MythTV. Guest accounts can create and edit rules but cannot delete rules or recordings. Apache protects public access, and the host firewall must restrict direct Flask access. Status summarizes backend version, connected encoders, and encoder state. Live TV lists programs currently airing on visible channels and offers the same schedule action; it does not start live playback because MythTV's HTTP services on this host do not expose a tuner-start or live-channel stream endpoint.

## Application accounts

The Flask app requires sign-in for the library and API; `/healthz` remains public. Accounts are stored in `.run/users.sqlite` with Werkzeug password hashes. On an empty account database, the app creates the initial administrator `admin` with password `mythtv-admin`; that account must change its password at first sign-in. Override the initial account before the first start with `MYTHTV_INITIAL_ADMIN_USERNAME` and `MYTHTV_INITIAL_ADMIN_PASSWORD`. The password setting is only used when the database has no users; it does not reset existing credentials. New accounts are also required to change their initial password. Admins manage users, roles, and passwords at `/admin/users`; supported roles are `admin`, `user`, and `guest`. Guest accounts otherwise behave like users but cannot delete recordings or recording rules.

`MYTHTV_AUTH_DATABASE` selects another account database path. Authentication cookies are persistent for 365 days by default and refresh while the user is active; set `MYTHTV_SESSION_LIFETIME_DAYS` to choose another lifetime. Flask creates and persists a random session-signing key under `.run/` by default; set `MYTHTV_SECRET_KEY` to supply a stable key explicitly, and keep it private. The selected key is persisted beside the account database so sessions survive restarts even if the environment variable is omitted later; changing the key signs out existing sessions. Set `MYTHTV_SESSION_COOKIE_SECURE=1` when all browser access uses HTTPS. For deployments using the known initial password, change it immediately on first sign-in; preferably set a unique `MYTHTV_INITIAL_ADMIN_PASSWORD` before the first start.

Existing account databases are migrated automatically on startup. The session-security upgrade requires existing users to sign in again. Password changes revoke other sessions while keeping the changing user's current session; administrator password resets revoke all sessions for the affected account. Session cookies use `SameSite=Strict` by default.

## Browser playback

Recordings are streamed through Flask from MythTV's `Content/GetRecording` service, preserving byte-range requests for seeking. The player uses MythTV commercial-break markers for smart/advert skipping and stores its resume position through `Dvr/SetLastPlayPos`. Recordings with browser-supported codecs play directly; this path does not require system FFmpeg.

System `ffprobe` is optional. It is the media-inspection companion to FFmpeg: MythTV Web uses it to identify stream codecs, frame rate, and subtitle tracks before playback. If it is missing or cannot inspect a recording, playback still attempts the browser's native decoder, but codec-based fallback selection, frame-rate metadata, and subtitle discovery may be unavailable.

System `ffmpeg` is also optional for basic playback, but is required for the server-side compatibility transcode and subtitle extraction. FFmpeg is a media toolkit that decodes, encodes, and remuxes audio/video; here, it converts an unsupported recording to fragmented H.264/AAC MP4 so the browser can play it. Without `ffmpeg`, browser-decodable recordings continue to work, but unsupported recordings cannot use this fallback. `ffmpeg` also extracts supported text subtitles as WebVTT; DVB bitmap subtitle tracks may not be convertible. These are system executables, not Python packages. Install the OS package on the MythTV Web host and ensure both `ffmpeg` and `ffprobe` are on Gunicorn's `PATH`. On Debian or Ubuntu, for example:

```sh
sudo apt install ffmpeg
```

Seeking within buffered media stays local; seeking outside the buffer restarts the server transcode at the requested position. Commercial skipping is available only when MythTV has commercial-break markers for the recording.

Server transcoding uses Linux process identity and pidfds for safe cancellation across workers. Jobs belong to the starting account session and recording. At most two transcodes run concurrently by default; additional requests receive `503` until a slot is free. Set `MYTHTV_MAX_TRANSCODES` to change this limit and `MYTHTV_FFMPEG_THREADS` to change the decoder, encoder, and filter thread budgets (default: two each). Both settings must be positive integers. These limits leave Gunicorn threads available for navigation and account requests.

Successful subtitle extractions for completed recordings are cached under `.run/subtitles/` for up to 24 hours, retaining at most 128 files. The cache key includes the recording's file metadata, source URL, and subtitle stream. Concurrent requests for the same track share an extraction; recordings still in progress are extracted afresh. Set `MYTHTV_SUBTITLE_CACHE_DIR` to select another cache directory.

### TODO: Revisit FFmpeg.wasm

FFmpeg.wasm is the FFmpeg media toolkit compiled to WebAssembly, a portable format that lets the browser run compiled code without installing a native FFmpeg program on the client. If enabled, MythTV Web can send the recording stream through Flask to the browser, convert it there to fragmented MP4, and feed the result to the video element with MediaSource. That can avoid server-side transcoding and help devices whose browser cannot decode the recording's original codecs.

The repository contains an experimental implementation, but it is currently disabled by `BROWSER_CONVERSION_ENABLED = false`; normal playback does not load or use the WASM code or core. The experiment uses a 1 MiB input ring and 4 MiB output ring backed by `SharedArrayBuffer`, and appends output to MediaSource in chunks so it does not buffer the entire recording in memory. It can copy compatible High Profile H.264 video; other video is encoded as H.264, and audio is encoded as AAC. If WASM conversion fails, the current code falls back to server-side FFmpeg, so enabling it would not remove the server FFmpeg requirement for that fallback.

The tradeoffs are significant: the browser must download the large WASM core when conversion starts, and transcoding can consume substantial CPU, memory, battery, and thermal capacity. Performance and codec support can vary widely on mobile devices and Android TV hardware. The shared-memory path also requires a cross-origin-isolated page; Flask already sends `Cross-Origin-Opener-Policy: same-origin` and `Cross-Origin-Embedder-Policy: require-corp`, and any reverse proxy must preserve them.

Before enabling the feature, make its selection capability-based, confirm the build artifacts are deployed, test conversion and seeking on target desktop/mobile/TV browsers, measure resource use on lower-powered devices, and verify cancellation and error fallback. Keep the server fallback or provide a clear playback error for devices that cannot run the WASM path.

## Quality checks

Run these checks before committing changes:

```sh
uv run ruff format --check .
uv run ruff check .
uv run pytest
npm run test:browser
npm run build:browser
```

## Run

For local development:

```sh
uv run mythtv-web
```

For the Apache-facing server process:

```sh
uv run gunicorn --workers 2 --worker-class gthread --threads 8 --timeout 120 --bind 0.0.0.0:6555 'mythtv_web:create_app()'
```

The landing page is at `/`; `/healthz` returns a small JSON health response. Stop the process with Ctrl+C when running it in the foreground.

In VS Code, use **Terminal > Run Task > uv: run app** to run the same command. For debugging, select **Python: MythTV-Web** in Run and Debug and press **F5**. The launch configuration syncs dependencies first, then uses the workspace's `.venv/bin/python` and serves `http://127.0.0.1:6555/`. Build browser assets using the quick-start commands above before the first run. Stop any server already using port 6555 before starting another instance.

## Background service

Use the launcher to manage Gunicorn in the background. It records its PID and writes logs under the ignored `.run/` directory:

```sh
./scripts/mythtv-web.sh start
./scripts/mythtv-web.sh status
./scripts/mythtv-web.sh restart
./scripts/mythtv-web.sh stop
```

The root-level `launch_server.sh` accepts the same commands and delegates to this service manager.
The launcher requires dependencies to be installed in `.venv/`. It only stops the Gunicorn process identified by its own PID file and refuses to signal a different process. Gunicorn access and error output is written to `.run/mythtv-web-YYYY-MM-DDT000000.log`, where the timestamp is the start of that log day in the server's local timezone. Rotation happens at local midnight; at most 10 dated log files are retained, including the active file. Existing logs with the previous `gunicorn-` prefix are migrated when the service starts.

## Apache reverse proxy

The example Apache snippet is [`config/apache/mythtv-web.conf`](config/apache/mythtv-web.conf). It serves the app at `https://mythtv.example.com/mythtv-web/` and redirects HTTP to HTTPS. Authentication is handled by the Flask app; Apache does not add a second login prompt. Replace `mythtv.example.com` in the snippet with the canonical DNS name covered by your HTTPS certificate. The Flask/Gunicorn service binds to all interfaces on port 6555; Apache forwards the `/mythtv-web/` prefix so the page's static files and API requests use the same path. The Apache snippet includes `ProxyPassReverseCookiePath / /mythtv-web/` to scope Flask's session cookie to this application path.

On Debian or Ubuntu, install the snippet as root:

```sh
sudo a2enmod proxy proxy_http headers rewrite
sudo install -o root -g root -m 644 config/apache/mythtv-web.conf /etc/apache2/conf-available/mythtv-web.conf
```

Include the snippet inside both the HTTP and HTTPS `<VirtualHost>` blocks for your chosen hostname in the Apache site configuration:

```apache
Include /etc/apache2/conf-available/mythtv-web.conf
```

Apache proxies requests without Basic Auth; Flask requires an application account for pages and APIs, except `/healthz` and static assets. The example assumes Apache and Flask run on the same host. Because Flask binds to `0.0.0.0:6555`, enforce the network boundary with the host firewall and allow only intended trusted clients or subnets to reach port 6555. Recording-file deletion still requires explicit confirmation and uses MythTV's non-forced delete options.

Start or restart the Flask service, then validate and reload Apache:

```sh
./scripts/mythtv-web.sh restart
sudo apache2ctl -t
sudo systemctl reload apache2
```

An unauthenticated browser request should redirect to the Flask login page; unauthenticated API requests return `401`. After sign-in, the homepage and its `/mythtv-web/static/` assets should load. Use HTTPS whenever credentials cross a network.

## Nginx reverse proxy

Nginx can serve the same `/mythtv-web/` URL prefix. Add a port 80 redirect and the following locations to the site's HTTPS `server` block, replacing the hostname and certificate paths with your own. Apply the session-cookie path rewrite in the reverse proxy that directly fronts Flask: use this Nginx directive when Nginx is that proxy, or Apache's `ProxyPassReverseCookiePath` above when Apache is; do not configure both for the same response.

```nginx
server {
	listen 80;
	server_name mythtv.example.com;
	return 301 https://$host$request_uri;
}

server {
	listen 443 ssl;
	server_name mythtv.example.com;
	ssl_certificate /etc/letsencrypt/live/mythtv.example.com/fullchain.pem;
	ssl_certificate_key /etc/letsencrypt/live/mythtv.example.com/privkey.pem;

	location = /mythtv-web {
		return 301 /mythtv-web/;
	}

	location /mythtv-web/ {
		proxy_pass http://127.0.0.1:6555/;
		proxy_cookie_path / /mythtv-web/;
		proxy_http_version 1.1;
		proxy_set_header Host $host;
		proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
		proxy_set_header X-Forwarded-Proto $scheme;
		proxy_set_header X-Forwarded-Prefix /mythtv-web;
		proxy_buffering off;
		proxy_read_timeout 120s;
	}
}
```

The trailing slash on `proxy_pass` removes `/mythtv-web/` before forwarding the request; `X-Forwarded-Prefix` lets Flask generate URLs with that prefix again. Nginx passes Flask's response headers through by default, including the cross-origin isolation headers used by the browser playback code. Flask authentication remains enabled, and the host firewall must still restrict direct access to port 6555. Validate and reload Nginx after installing the configuration:

```sh
sudo nginx -t
sudo systemctl reload nginx
```

