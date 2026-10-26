"""Controller-only GitHub App registration and narrowly scoped token issuance."""
import base64
import html
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import time
import urllib.parse
import urllib.request

API = "https://api.github.com"


def request(path, token=None, payload=None):
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "coder-aws",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = json.dumps(payload).encode() if payload is not None else None
    with urllib.request.urlopen(urllib.request.Request(API + path, data=data, headers=headers), timeout=30) as response:
        return json.load(response)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix(".tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as target:
        target.write(value)
    os.replace(temporary, path)


def jwt(directory):
    app = json.loads((directory / "app.json").read_text())
    def encode(value):
        return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=")
    now = int(time.time())
    message = encode({"alg": "RS256", "typ": "JWT"}) + b"." + encode({
        "iat": now - 60, "exp": now + 540, "iss": str(app["id"])})
    signed = subprocess.run(["openssl", "dgst", "-sha256", "-sign", str(directory / "private-key.pem")],
                            input=message, capture_output=True, check=True).stdout
    return (message + b"." + base64.urlsafe_b64encode(signed).rstrip(b"=")).decode()


def repository(value):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_.-]+", value):
        raise ValueError("Repository must be OWNER/REPO")
    owner, name = value.split("/")
    if name in (".", ".."):
        raise ValueError("Invalid repository name")
    return owner, name


def mint(directory, repo, write=False):
    owner, name = repository(repo)
    app_token = jwt(directory)
    installation = request("/repos/" + owner + "/" + name + "/installation", app_token)
    # GitHub itself enforces membership in this App installation; each request
    # further narrows the resulting credential to exactly one repository.
    return request(f"/app/installations/{installation['id']}/access_tokens", app_token,
                   {"repositories": [name], "permissions": {"contents": "write" if write else "read"}})


def manifest(base, nonce, app_name="AWS Workspaces"):
    return {"name": app_name, "url": "https://coder.com",
            "description": "Repository-scoped access for AWS workspaces",
            "public": False, "default_permissions": {"contents": "write", "metadata": "read"},
            "default_events": [], "request_oauth_on_install": False,
            "redirect_url": base + "/callback", "setup_url": base + "/installed?state=" + nonce}


def login_manifest(base, nonce, app_name, coder_url):
    return {"name": app_name + " Login", "url": coder_url,
            "description": "Sign in to your private Coder dashboard",
            "public": False, "default_permissions": {"email_addresses": "read"},
            "default_events": [], "request_oauth_on_install": False,
            "redirect_url": base + "/callback",
            "callback_urls": [coder_url + "/api/v2/users/oauth2/github/callback"]}


def login_environment(app, coder_url):
    values = {"CODER_OAUTH2_GITHUB_CLIENT_ID": app["client_id"],
              "CODER_OAUTH2_GITHUB_CLIENT_SECRET": app["client_secret"],
              "CODER_ACCESS_URL": coder_url,
              "CODER_OAUTH2_GITHUB_DEVICE_FLOW": "false",
              "CODER_OAUTH2_GITHUB_DEFAULT_PROVIDER_ENABLE": "false",
              "CODER_OAUTH2_GITHUB_ALLOW_EVERYONE": "true",
              "CODER_OAUTH2_GITHUB_ALLOW_SIGNUPS": "false"}
    return "".join(key + "=" + json.dumps(value) + "\n" for key, value in values.items())


def setup(directory, port=8786, owner="", app_name="AWS Workspaces", login_url=None):
    """Serve a bounded loopback-only consent flow; never log callback secrets."""
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    nonce = secrets.token_urlsafe(32)
    base = f"http://127.0.0.1:{port}"
    completed = False

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def page(self, body, status=200):
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(("<!doctype html><meta charset=utf-8><title>Coder GitHub setup</title>" + body).encode())

        def do_GET(self):
            nonlocal completed
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if self.headers.get("Host") != f"127.0.0.1:{port}" or query.get("state") != [nonce]:
                self.page("Invalid setup session. Return to the setup URL.", 403)
                return
            try:
                if parsed.path == "/":
                    if login_url and (directory / "login.env").exists():
                        self.page("<p>Dashboard login is already configured. Restart Coder to apply saved credentials.</p>")
                        completed = True
                    elif (directory / "app.json").exists():
                        app = json.loads((directory / "app.json").read_text())
                        self.page('<a href="' + html.escape(app["html_url"] + "/installations/new") + '">Install the existing App on your repositories</a>')
                    else:
                        definition = login_manifest(base, nonce, app_name, login_url) if login_url else manifest(base, nonce, app_name)
                        value = html.escape(json.dumps(definition), quote=True)
                        intro = ("<h1>GitHub dashboard login</h1><p>Create your own login App. It requests read-only email access, with no repository access. Credentials are saved locally.</p>"
                                 if login_url else '<h1>GitHub access for AWS workspaces</h1><p>Create a private App in your main account, '
                                  'then install it on all or selected repositories. The App permits contents read/write; worker '
                                  'tokens default to read-only and write tokens require an explicit agent command. '
                                  'Commit authors remain your configured agent account.</p>')
                        self.page(intro +
                                  '<form method="post" action="https://github.com/settings/apps/new?state=' + nonce + '">'
                                  '<input type="hidden" name="manifest" value="' + value + '">'
                                  '<button>Create GitHub App</button></form>')
                elif parsed.path == "/callback" and not (directory / "app.json").exists():
                    code = query.get("code", [""])[0]
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,200}", code):
                        raise ValueError("Invalid registration code")
                    app = request("/app-manifests/" + code + "/conversions", payload={})
                    if not app["html_url"].startswith("https://github.com/apps/"):
                        raise ValueError("Unexpected App URL")
                    if login_url:
                        save(directory / "login.env", login_environment(app, login_url))
                        save(directory / "login-app.json", json.dumps({k: app[k] for k in ("id", "slug", "html_url")}))
                        self.page("<p>Login App created. Credentials saved privately. You can return to the agent to finish switching Coder.</p>")
                        completed = True
                        return
                    save(directory / "private-key.pem", app["pem"])
                    save(directory / "app.json", json.dumps({k: app[k] for k in ("id", "slug", "html_url")}, indent=2))
                    self.page('<p>App created. Its private key is saved locally.</p><a href="' +
                              html.escape(app["html_url"] + "/installations/new") + '">Install on selected repositories</a>')
                elif parsed.path == "/installed":
                    installation_id = query.get("installation_id", [""])[0]
                    if not installation_id.isdecimal():
                        raise ValueError("Missing installation ID")
                    installation = request("/app/installations/" + installation_id, jwt(directory))
                    account = installation["account"]["login"]
                    if owner and account.lower() != owner.lower():
                        raise ValueError("Install the App on the configured account")
                    save(directory / "installation.json", json.dumps({"id": int(installation_id), "owner": account}))
                    self.page("<p>GitHub App installed. Repository-scoped token issuance is ready.</p>")
                    completed = True
                else:
                    self.page("Unknown setup step", 404)
            except Exception:
                # Do not send API responses, codes, PEM or token data to logs/browser.
                self.page("Setup could not finish. Retry from the original setup URL; saved App credentials are retained.", 400)

    with HTTPServer(("127.0.0.1", port), Handler) as server:
        server.timeout = 1
        print(base + "/?state=" + nonce, flush=True)
        deadline = time.monotonic() + 900
        while not completed and time.monotonic() < deadline:
            server.handle_request()
    if not completed:
        raise RuntimeError("GitHub setup window expired; run github-setup again to resume")
