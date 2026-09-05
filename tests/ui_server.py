"""A disposable server for browser tests; never connects to a user's app."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.request


@contextmanager
def isolated_server():
    with tempfile.TemporaryDirectory(prefix="gusnb-ui-") as temporary:
        root = Path(temporary)
        work, state = root / "work", root / "state"
        work.mkdir()
        token = secrets.token_urlsafe(32)
        # Bind port zero in the server itself, avoiding a choose/release race.
        startup = root / "startup.json"
        env = {**os.environ, "GUSNOTEBOOK_HOME": str(state),
               "GUSNOTEBOOK_TOKEN": token, "NO_LLM": "1",
               "GUSNOTEBOOK_TEST_ROOT": str(root), "NB_TOKEN": token}
        for key in ("NOTEBOOK", "GUSNOTEBOOK_NO_AUTH", "AI_GATEWAY_KEY", "AI_GATEWAY_URL",
                    "AWS_BEARER_TOKEN_BEDROCK", "NB_URL"):
            env.pop(key, None)
        env.update(AI_GATEWAY_URL="http://127.0.0.1:9", AI_GATEWAY_KEY="test-gateway-key")
        command = [sys.executable, str(Path(__file__).resolve()), "serve", str(work), str(state), str(startup)]
        with (root / "server.log").open("w+") as log:
            server = subprocess.Popen(command, env=env, stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while not startup.exists():
                    if server.poll() is not None or time.monotonic() > deadline:
                        log.seek(0)
                        raise RuntimeError("Test server failed to start:\n" + log.read())
                    time.sleep(0.05)
                url = json.loads(startup.read_text())["url"]
                env.update(GUSNOTEBOOK_TEST_URL=url, GUSNOTEBOOK_ISOLATED_TEST="1", NB_URL=url.rstrip("/"))
                yield url, token, root, env
            finally:
                server.terminate()
                try:
                    server.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()


def rerun_isolated(script):
    if os.environ.get("GUSNOTEBOOK_ISOLATED_TEST") == "1":
        return False
    with isolated_server() as (_url, _token, _root, env):
        result = subprocess.run([sys.executable, str(Path(script).resolve())], env=env)
    raise SystemExit(result.returncode)


def launch_browser(playwright):
    channel = os.environ.get("GUSNOTEBOOK_BROWSER_CHANNEL")
    if not channel and not Path(playwright.chromium.executable_path).exists():
        if Path("/Applications/Google Chrome.app").exists():
            channel = "chrome"
    return playwright.chromium.launch(headless=True, **({"channel": channel} if channel else {}))


def authenticate_browser(context, url):
    response = context.request.post(url.rstrip("/") + "/auth", headers={
        "Authorization": "Bearer " + os.environ["NB_TOKEN"]})
    if response.status != 200:
        raise RuntimeError("Could not authenticate disposable browser")


def serve(work, state, startup):
    import signal
    import threading
    from werkzeug.serving import make_server
    from gusnotebook.app import create_app, close_app
    app = create_app({"WORK_DIR": work, "STATE_DIR": state})
    server = make_server("127.0.0.1", 0, app, threaded=True)
    url = f"http://127.0.0.1:{server.server_port}/"
    app.config["APP_URL"] = url.rstrip("/")
    Path(startup).write_text(json.dumps({"url": url}))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=server.shutdown).start())
    try:
        server.serve_forever()
    finally:
        close_app(app)
        server.server_close()


if __name__ == "__main__":
    serve(*sys.argv[2:])
