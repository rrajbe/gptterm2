import os
from dataclasses import dataclass, field


def _bool(v):
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    host: str = "127.0.0.1"
    port: int = 8000
    cdp_url: str = "http://127.0.0.1:9222"
    launch_browser: bool = False
    dev_port: int = 9335
    dev_profile: str = os.path.expanduser("~/Library/Application Support/gptapi-dev/chrome-profile")
    data: str = "./data"
    api_key: str = ""
    max_tabs: int = 4
    novnc_url: str = "http://localhost:6080"
    raw: bool = False
    log_level: str = "INFO"
    agent_mode: bool = True            # emulate tool calling when a request carries tools
    tools_allow: tuple = ("edit", "write", "patch", "apply_patch", "glob", "grep", "read", "list", "shell")  # "*" = all
    tool_result_chars: int = 8000      # longer tool results are cut (head + tail)
    rollover_chars: int = 70000        # start a fresh chatgpt.com conversation past this size

    @classmethod
    def from_env(cls, env=None):
        e = os.environ if env is None else env
        c = cls()
        c.host = e.get("GPTAPI_HOST", c.host)
        c.port = int(e.get("GPTAPI_PORT", c.port))
        c.launch_browser = _bool(e.get("GPTAPI_LAUNCH_BROWSER"))
        c.dev_port = int(e.get("GPTAPI_DEV_PORT", c.dev_port))
        # dev mode always talks to the Chrome it launched itself (never CDP_URL)
        c.cdp_url = f"http://127.0.0.1:{c.dev_port}" if c.launch_browser else e.get("CDP_URL", c.cdp_url)
        c.data = os.path.abspath(os.path.expanduser(e.get("GPTAPI_DATA", c.data)))
        c.api_key = e.get("GPTAPI_KEY", "")
        c.max_tabs = max(1, int(e.get("GPTAPI_MAX_TABS", c.max_tabs)))
        c.novnc_url = e.get("GPTAPI_NOVNC_URL", c.novnc_url)
        c.raw = _bool(e.get("GPTAPI_RAW"))
        c.log_level = e.get("GPTAPI_LOG_LEVEL", c.log_level).upper()
        if "GPTAPI_AGENT_MODE" in e:
            c.agent_mode = _bool(e["GPTAPI_AGENT_MODE"])
        if e.get("GPTAPI_TOOLS"):
            c.tools_allow = tuple(x.strip() for x in e["GPTAPI_TOOLS"].split(",") if x.strip())
        c.tool_result_chars = int(e.get("GPTAPI_TOOL_RESULT_CHARS", c.tool_result_chars))
        c.rollover_chars = int(e.get("GPTAPI_ROLLOVER_CHARS", c.rollover_chars))
        if c.launch_browser and c.dev_port == 9333:
            raise SystemExit("GPTAPI_DEV_PORT 9333 is reserved for gptterm; pick another port")
        return c
