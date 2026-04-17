from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from rich.console import Console
from datetime import datetime
from http import HTTPStatus
import urllib.parse
import subprocess
import posixpath
import mimetypes
import ipaddress
import socket
import time
import re
import os

console = Console(highlight=False)
mimetypes.init()

class log:
    def __init__(self):
        self._last = time.perf_counter()

    def __call__(self, message):
        now = time.perf_counter()
        delta_ms = (now - self._last) * 1000
        self._last = now
        console.print(f"[blue][+{delta_ms:.2f}ms] [reset]{message}")

    def resettimer(self):
        self._last = time.perf_counter()
log = log()

log("initializing functions")

def abspath(base_dir, p):
    if not p:
        return ""
    if os.path.isabs(p):
        return os.path.normpath(p)
    return os.path.normpath(os.path.join(base_dir, p))

def run_git(args, cwd=None):
    p = subprocess.run(
        ["git"] + args,
        cwd=cwd,
        capture_output=True,
        text=True
    )
    if p.returncode != 0:
        msg = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {msg}")
    return (p.stdout or "").strip()

def git_ensure_updated(repo_url, branch, dest_dir):
    if not repo_url:
        return

    if not dest_dir:
        raise RuntimeError("GIT_REPO is set but GIT_DEST resolved to empty path")

    dest_dir = os.path.abspath(dest_dir)
    os.makedirs(dest_dir, exist_ok=True)

    git_dir = os.path.join(dest_dir, ".git")
    if not os.path.isdir(git_dir):
        parent = os.path.dirname(dest_dir.rstrip("\\/"))
        name = os.path.basename(dest_dir.rstrip("\\/"))
        os.makedirs(parent, exist_ok=True)

        if os.path.exists(dest_dir) and os.listdir(dest_dir):
            raise RuntimeError(f"GIT_DEST exists and is not empty: {dest_dir}")

        run_git(["clone", "--depth", "1", "--branch", branch, repo_url, name], cwd=parent)
        return

    local_head = run_git(["rev-parse", "HEAD"], cwd=dest_dir)
    remote_line = run_git(["ls-remote", repo_url, f"refs/heads/{branch}"])
    remote_head = remote_line.split()[0].strip() if remote_line else ""

    if not remote_head:
        raise RuntimeError(f"Could not resolve remote head for {branch}")

    if local_head != remote_head:
        run_git(["fetch", "origin", branch, "--depth", "1"], cwd=dest_dir)
        run_git(["reset", "--hard", f"origin/{branch}"], cwd=dest_dir)

def compile_blacklist(patterns):
    out = []
    for raw in (patterns or "").split(","):
        pat = raw.strip()
        if not pat:
            continue
        try:
            out.append(re.compile(pat))
        except re.error as e:
            raise RuntimeError(f"Invalid blacklist regex '{pat}': {e}")
    return out

def make_handler(html_root, blacklist):
    def _handler(*args, **kwargs):
        return StaticRouter(*args, html_root=html_root, blacklist=blacklist, **kwargs)
    return _handler

def resolve_bind_address(bind_addr):
    if bind_addr not in ("0.0.0.0", "::"):
        return [bind_addr]

    ips = []

    try:
        for iface in socket.getaddrinfo(socket.gethostname(), None):
            ip = iface[4][0]

            if "." not in ip:
                continue

            if ip.startswith("127."):
                continue

            ips.append(ip)

    except Exception as e:
        print(e)

    if bind_addr == "0.0.0.0":
        ips.append("127.0.0.1")
        
    return sorted(ips)

log("initializing StaticRouter")

class StaticRouter(SimpleHTTPRequestHandler):
    server_version = "WebServer/1.0"

    def __init__(self, *args, html_root=None, blacklist=None, **kwargs):
        self.html_root = html_root
        self.blacklist = blacklist or []
        super().__init__(*args, **kwargs)

    def send_response(self, code, message=None):
        self._last_response_code = int(code)
        self._last_response_message = message or HTTPStatus(int(code)).phrase
        super().send_response(code, message)

    def get_real_ip(self, proxies=[]):
        peer_ip = self.client_address[0]

        PROXIES = [
            "127.0.0.1",
            "::1",
            "192.168.0.0/16",
        ]
        PROXIES.append(proxies)
        
        def is_trusted(ip):
            try:
                ip_obj = ipaddress.ip_address(ip)
                for entry in PROXIES:
                    if "/" in entry:
                        if ip_obj in ipaddress.ip_network(entry, strict=False):
                            return True
                    else:
                        if ip == entry:
                            return True
            except Exception:
                return False
            return False

        if not is_trusted(peer_ip):
            return peer_ip

        cf_ip = self.headers.get("CF-Connecting-IP")
        if cf_ip:
            return cf_ip.strip()

        xff = self.headers.get("X-Forwarded-For")
        if xff:
            for part in xff.split(","):
                ip = part.strip()
                try:
                    ipaddress.ip_address(ip)
                    return ip
                except Exception:
                    continue

        x_real_ip = self.headers.get("X-Real-IP")
        if x_real_ip:
            return x_real_ip.strip()

        return peer_ip

    def log_request(self, code='-', size='-'):
        method = getattr(self, "command", "-")
        path = getattr(self, "path", "-")
        protocol = getattr(self, "request_version", "-")
        clientaddr = self.get_real_ip(self.client_address[0])
        dt = datetime.now().strftime("%m/%d/%Y@%I:%M:%S %p")

        try:
            code = int(code)
        except Exception:
            code = '-'

        if isinstance(code, int):
            if code // 100 == 2:
                color = "green"
            elif code // 100 == 3:
                color = "green"
            elif code // 100 == 4:
                color = "yellow"
            elif code // 100 == 5:
                color = "red"
            else:
                color = "white"
        else:
            color = "white"

        try:
            message = self._last_response_message
        except Exception:
            try:
                message = HTTPStatus(code).phrase if isinstance(code, int) else "-"
            except Exception:
                message = "-"

        console.print(f'[yellow]{clientaddr}[reset] - [[blue]{dt}[reset]] [{color}]"{method} {path} {protocol} -> {code} {message}"')

    def log_message(self, fmt, *args):
        pass

    def is_blacklisted(self, url_path):
        for rx in self.blacklist:
            if rx.search(url_path):
                return True
        return False

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        url_path = parsed.path or "/"

        if self.is_blacklisted(url_path):
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        target = self.resolve_target(url_path)
        if target is None:
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        if not os.path.isfile(target):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.serve_file(target)

    def do_HEAD(self):
        parsed = urllib.parse.urlsplit(self.path)
        url_path = parsed.path or "/"

        if self.is_blacklisted(url_path):
            self.send_error(HTTPStatus.FORBIDDEN)
            return

        target = self.resolve_target(url_path)
        if target is None or not os.path.isfile(target):
            self.send_error(HTTPStatus.NOT_FOUND)
            return

        self.serve_file(target, head_only=True)

    def resolve_target(self, url_path):
        safe_path = posixpath.normpath(urllib.parse.unquote(url_path))
        if safe_path.startswith("../") or safe_path.startswith("..\\"):
            return None

        if safe_path == "/":
            return os.path.join(self.html_root, "index.html")

        if url_path.endswith("/"):
            rel = safe_path.lstrip("/")
            full = os.path.normpath(os.path.join(self.html_root, rel))
            if os.path.isdir(full):
                i1 = os.path.join(full, "index.html")
                if os.path.isfile(i1):
                    return i1
                i2 = os.path.join(full, "index.htm")
                if os.path.isfile(i2):
                    return i2
            return None

        rel = safe_path.lstrip("/")
        full = os.path.normpath(os.path.join(self.html_root, rel))

        if not full.startswith(self.html_root):
            return None

        rel_ext = os.path.splitext(rel)[1]
        if rel_ext:
            if os.path.isfile(full):
                return full
            return None

        if os.path.isdir(full):
            i1 = os.path.join(full, "index.html")
            if os.path.isfile(i1):
                return i1
            i2 = os.path.join(full, "index.htm")
            if os.path.isfile(i2):
                return i2
            return None

        if os.path.isfile(full):
            return full

        html_fallback = os.path.normpath(os.path.join(self.html_root, rel + ".html"))
        if os.path.isfile(html_fallback):
            return html_fallback

        htm_fallback = os.path.normpath(os.path.join(self.html_root, rel + ".htm"))
        if os.path.isfile(htm_fallback):
            return htm_fallback

        return None

    def serve_file(self, filepath, head_only=False):
        ctype, _ = mimetypes.guess_type(filepath)
        if not ctype:
            ctype = "application/octet-stream"

        try:
            fs = os.stat(filepath)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(fs.st_size))
            self.send_header("Last-Modified", self.date_time_string(fs.st_mtime))
            self.end_headers()

            if head_only:
                return

            with open(filepath, "rb") as f:
                self.copyfile(f, self.wfile)
        except Exception as e:
            print(e)
            raise

    def send_error(self, code, message=None, explain=None):
        self._last_response_code = int(code)
        self._last_response_message = message or HTTPStatus(int(code)).phrase

        if 400 <= int(code) <= 599:
            try:
                raw_path = urllib.parse.urlsplit(self.path).path or "/"
                if raw_path.endswith("/"):
                    dir_path = raw_path
                else:
                    dir_path = posixpath.dirname(raw_path) or "/"

                rel_dir = dir_path.lstrip("/")
                cur_dir = os.path.normpath(os.path.join(self.html_root, rel_dir))

                if not cur_dir.startswith(self.html_root):
                    cur_dir = self.html_root

                while True:
                    err_file = os.path.join(cur_dir, f"{int(code)}.html")
                    if os.path.isfile(err_file):
                        ctype, _ = mimetypes.guess_type(err_file)
                        if not ctype:
                            ctype = "text/html; charset=utf-8"

                        with open(err_file, "rb") as f:
                            data = f.read()

                        self.send_response(code)
                        self.send_header("Content-Type", ctype)
                        self.send_header("Content-Length", str(len(data)))
                        self.end_headers()
                        self.wfile.write(data)
                        return

                    if cur_dir == self.html_root:
                        break

                    parent = os.path.dirname(cur_dir)
                    if not parent or parent == cur_dir:
                        break
                    if not parent.startswith(self.html_root):
                        break
                    cur_dir = parent
            except Exception as e:
                print(e)

        super().send_error(code, message, explain)

def main():
    log("loading env")
    host=os.environ.get("INTERNAL_IP", os.environ.get("HOST", "127.0.0.1"))
    port=int(os.environ.get("SERVER_PORT", 80))
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    html_root = abspath(base_dir, os.environ.get("HTML_ROOT", "./html"))
    os.makedirs(html_root, exist_ok=True)

    blacklist = compile_blacklist(os.environ.get("BLACKLIST", ""))
    
    git_repo = os.environ.get("GIT_REPO", "").strip()
    git_branch = os.environ.get("GIT_BRANCH", "main").strip() or "main"
    git_dest_env = os.environ.get("GIT_DEST", "").strip()

    git_dest = html_root
    if git_dest_env:
        if os.path.isabs(git_dest_env):
            git_dest = git_dest_env
        else:
            git_dest = abspath(html_root, git_dest_env)

    if git_repo:
        log("loading git")

        try:
            git_ensure_updated(git_repo, git_branch, git_dest)
            log(f"synced {git_repo} ({git_branch}) -> {git_dest}")
        except Exception as e:
            log(f"update failed: {e}")
            raise
    
    log(f"initializing http.server on [yellow]{host}{'' if port == 80 else f':{port}'}[reset]")
    
    try:
        httpd = ThreadingHTTPServer((host, port), make_handler(html_root, blacklist))
    except KeyboardInterrupt:
        log("Stopping server")
        return
        
    print("Serving on addresses:")
    for host in sorted(resolve_bind_address(host), key=len, reverse=True):
        console.print(f"  [#0000EE]http://{host}{'' if port == 80 else f':{port}'}/")
    print()
    
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.resettimer()
        log("Stopping server")
    finally:
        httpd.shutdown()
        httpd.server_close()

        
if __name__ == "__main__":
    main()
    log("Server closed.")
