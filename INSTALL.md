# Installing gptapi on another computer

gptapi runs in one Docker container: a real Chromium on chatgpt.com (logged out) plus a small
server that offers it as an OpenAI-compatible API at `http://127.0.0.1:8000/v1`. You use it
from opencode, or from any client that speaks the OpenAI chat API.

## 1. What the computer needs

| Need | Notes |
|---|---|
| macOS (Apple Silicon or Intel) or Linux | Tested on an Apple Silicon Mac. |
| **Docker Desktop**, running | https://www.docker.com/products/docker-desktop/ (Linux: Docker Engine with the compose plugin). The container needs about 1.5 GB of RAM, and Docker Desktop's default memory setting is enough. |
| About 3 GB of free disk | The image is about 1.2 GB. Browser data and transcripts grow slowly. |
| Internet access to chatgpt.com | No account is needed. |
| **opencode** (optional) | `brew install opencode` (needs Homebrew from https://brew.sh). Only needed if you want to use opencode. |

Nothing else is needed on the computer itself: Python, Chromium and everything else live
inside the image.

## 2. Copy and unpack

Copy `gptapi-0.0.1.tar.gz` (and its `.sha256`) to the computer, then:

```bash
shasum -a 256 -c gptapi-0.0.1.tar.gz.sha256
```

```bash
tar xzf gptapi-0.0.1.tar.gz && cd gptapi-0.0.1
```

## 3. Install and start

Open Docker Desktop first and wait until it shows "Engine running". Then:

```bash
./install.sh --test
```

The script:
1. checks Docker,
2. builds the image (3–5 minutes the first time),
3. starts the container `gptapi`, which restarts by itself after a reboot once Docker Desktop is running,
4. waits until the API answers,
5. with `--test`, sends one message and prints the answer (`ready`),
6. sets up opencode (step 4).

Leave out `--test` to skip the test message; it uses one of chatgpt.com's logged-out
messages. You can run `./install.sh` again at any time: it rebuilds and restarts the
container and keeps all data.

If port 8000 or 6080 is already in use on that computer, pick others. The installer puts the
same port into the opencode config it writes:

```bash
GPTAPI_API_PORT=8001 GPTAPI_NOVNC_PORT=6081 ./install.sh
```

If you use other ports, set them the same way every time you run `docker compose` commands
yourself (step 6), or put them in a `docker/.env` file:

```
GPTAPI_API_PORT=8001
GPTAPI_NOVNC_PORT=6081
```

## 4. opencode

`opencode.json` in this folder is the clean config:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "providers": {
    "gptapi": {
      "name": "ChatGPT web (gptapi)",
      "package": "@ai-sdk/openai-compatible",
      "settings": {
        "baseURL": "http://127.0.0.1:8000/v1"
      },
      "models": {
        "chatgpt-web": {
          "name": "ChatGPT (logged-out web)"
        }
      }
    }
  },
  "model": "gptapi/chatgpt-web",
  "agents": {
    "title": {
      "disabled": true
    }
  }
}
```

| Entry | Meaning |
|---|---|
| `$schema` | Lets editors check the file and autocomplete it. |
| `providers.gptapi` | A model server called `gptapi`. The name is the part before `/` in `gptapi/chatgpt-web`. |
| `name` | The label shown in opencode's model picker. |
| `package` | The adapter opencode downloads to talk to any OpenAI-compatible server. |
| `settings.baseURL` | Where gptapi listens. Change the port if you installed on another port. |
| `models.chatgpt-web` | The one model gptapi offers. `name` is its label in the picker. |
| `model` | The default model, so plain `opencode` uses gptapi. |
| `agents.title.disabled` | Turns off opencode's automatic session titles. Each title would otherwise send an extra chatgpt.com message and use up the logged-out message limit faster. Sessions keep a default title instead. |

There's no API key, because gptapi only listens on this computer.

This file uses opencode 2.x's current key names (`providers`, `package`, `settings`,
`agents`). The older names (`provider`, `npm`, `options`, `small_model`) still work, but
opencode marks them deprecated. It was tested with opencode 2.4.0.

**How the installer handles it:**
- **No opencode config yet:** it copies this file to `~/.config/opencode/opencode.json`.
- **A config that already has a `gptapi` provider:** it leaves the file alone.
- **Any other existing config:** it leaves the file alone and tells you. Add the `gptapi`
  block under `"providers"` yourself. Keep your current `"model"` if you want another provider
  as the default; you can still pick gptapi in the model picker or with
  `opencode -m gptapi/chatgpt-web`.

After editing the config by hand, restart opencode's background server:

```bash
opencode service restart
```

Check it:

```bash
opencode models | grep gptapi
```

```bash
opencode run "Say hello in five words"
```

## 5. Human checks ("verify you are human")

Usually there are none: the page passes Cloudflare's check by itself. If chatgpt.com ever
asks for one, requests fail with `human_check_required`. Then open the browser view at
http://127.0.0.1:6080 (it opens noVNC; click **Connect** if asked), click the check in the
Chromium window, and send your message again. Don't sign in there: gptapi works logged out.

## 6. Everyday commands

Run these from the `gptapi-0.0.1` folder.

| What | Command |
|---|---|
| Is it running and healthy? | `docker ps --filter name=gptapi` and `curl -s http://127.0.0.1:8000/health` |
| API log | `docker exec gptapi tail -f /var/log/gptapi/api.log` |
| Stop (keeps data) | `docker compose -f docker/compose.yaml stop` |
| Start again | `docker compose -f docker/compose.yaml start` |
| Restart | `docker restart gptapi` |
| Update to a new version | unpack the new archive and run its `./install.sh` (the data volume carries over) |
| Remove the container (keeps data) | `docker compose -f docker/compose.yaml down` |
| Remove everything, including data | `docker compose -f docker/compose.yaml down -v` and `docker image rm gptapi:latest` |

Conversations continue across restarts, updates and reboots: gptapi saves each conversation's
state and picks it up in a fresh browser tab.

## 7. Where the data is

Everything is in the Docker volume `gptapi-data`:

| Path in the container | What |
|---|---|
| `/data/chrome-profile` | Chromium profile (cookies) |
| `/data/gptapi/state.json` | Conversation state, names, which tab holds which conversation |
| `/data/gptapi/sessions/<conversation-id>.jsonl` | Transcripts, one JSON line per message |
| `/data/gptapi/debug/` | A screenshot and page text for every failed request |
| `/data/gptapi/gptapi.log` | API log |

Copy the transcripts out with:

```bash
docker cp gptapi:/data/gptapi/sessions ./gptapi-transcripts
```

## 8. Troubleshooting

| Symptom | Fix |
|---|---|
| `Docker is installed but not running` | Start Docker Desktop and wait for "Engine running". |
| `Port 8000 is already in use` | Install on other ports (step 3). |
| opencode: `Model unavailable: gptapi/chatgpt-web` | Run `opencode service restart`, then check `opencode models | grep gptapi`. |
| Errors with `rate_limit_exceeded` (HTTP 429) | chatgpt.com's logged-out message limit is reached. The error text says what the page shows. Try again later. |
| Errors with `human_check_required` (HTTP 503) | Clear the check at http://127.0.0.1:6080 (step 5). |
| Errors with `browser_unavailable` (HTTP 503) | `docker restart gptapi`. If it keeps happening, check `docker exec gptapi tail -50 /var/log/gptapi/chromium.log`. |
| The answer looks cut off or wrong | Watch the browser at http://127.0.0.1:6080 while sending; the last failure is saved in `/data/gptapi/debug/`. |

What gptapi supports, and its limits (chat only, no tool calling yet), are in
[README.md](README.md).
