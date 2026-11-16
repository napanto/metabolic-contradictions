#!/usr/bin/env python3
"""OS-isolated, subscription-only Codex oracle; inspection never invokes inference.

The prompt is supplied only through stdin. No project/user directory is mounted.
"""
from __future__ import annotations

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
import select
import shutil
import signal
import socket
import socketserver
import subprocess
import tempfile
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MODEL = "gpt-6-astra"
EFFORT = "max"
PROTOCOL_VERSION = "chl-oracle-isolation-v1"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ALLOWED_CONNECT_HOSTS = frozenset({"chatgpt.com"})
CATALOG_TOOL_OVERRIDES = {
    "node_repl_disabled": True,
    "apply_patch_tool_type": None,
    "experimental_supported_tools": [],
    "tool_mode": "direct",
}
DISABLED_FEATURES = (
    "shell_tool", "view_image", "apps", "plugins", "remote_plugin",
    "plugin_discovery_prefetch", "plugin_sharing", "recommended_plugins",
    "hooks", "memories", "multi_agent", "multi_agent_v2", "goals",
    "browser_use", "browser_use_external", "browser_use_full_cdp_access",
    "browser_annotation_api", "computer_use", "in_app_browser",
    "in_app_chat", "in_app_local_automation", "image_generation",
    "skill_search", "skill_mcp_dependency_install", "shell_snapshot",
    "code_mode", "code_mode_host", "tool_suggest", "sleep_tool",
    "daemon_auto_start", "workspace_dependencies", "external_agent_memory_import",
    "api_key_model_discovery", "api_key_cyber_access_programs",
    "system_proxy_fallback", "unbounded_connection_retries",
)
CONFIG = {
    "model": MODEL,
    "model_provider": "openai",
    "model_reasoning_effort": EFFORT,
    "forced_login_method": "chatgpt",
    "cli_auth_credentials_store": "file",
    "approval_policy": "never",
    "sandbox_mode": "read-only",
    "web_search": "disabled",
    "model_instructions_file": "/isolation/model-instructions.txt",
    "model_catalog_json": "/isolation/model-catalog.json",
    "include_environment_context": False,
    "include_environment_context_time": False,
    "include_permissions_instructions": False,
    "include_apps_instructions": False,
    "include_collaboration_mode_instructions": False,
    "personality": "none",
    "agents.enabled": False,
    "project_doc_max_bytes": 0,
    "history.persistence": "none",
    "skills.bundled.enabled": False,
    "skills.include_instructions": False,
    "plugins._default.enabled": False,
    "tools.update_plan.enabled": False,
    "tools.experimental_request_user_input.enabled": False,
    "analytics.enabled": False,
    "feedback.enabled": False,
    "shell_environment_policy.inherit": "none",
    **{f"features.{name}": False for name in DISABLED_FEATURES},
}


@dataclass(frozen=True)
class OracleResult:
    stdout: str
    stderr: str
    output: str
    returncode: int
    metadata: dict[str, Any]


def _config_args(effort: str = EFFORT) -> list[str]:
    return [part for key, value in {**CONFIG, "model_reasoning_effort": effort}.items()
            for part in ("-c", f"{key}={json.dumps(value)}")]


def _sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _auth_snapshot(timeout: float) -> bytes:
    """Read subscription credentials without logging or copying an API key/refresh token."""
    auth_path = Path.home() / ".codex" / "auth.json"
    data = json.loads(auth_path.read_bytes())
    if data.get("auth_mode") != "chatgpt":
        raise RuntimeError("ChatGPT subscription login is required; API-key fallback is forbidden")
    tokens = data.get("tokens") or {}
    access = tokens.get("access_token", "")
    try:
        claims = json.loads(base64.urlsafe_b64decode(access.split(".")[1] + "==="))
        expires = float(claims["exp"])
    except (IndexError, KeyError, ValueError, TypeError) as exc:
        raise RuntimeError("Cannot verify the subscription access-token lifetime") from exc
    if expires < time.time() + timeout + 300:
        raise RuntimeError("Subscription access token expires too soon; refresh outside the oracle")
    refresh = data.get("last_refresh")
    if not refresh:
        raise RuntimeError("Subscription credential freshness metadata is missing")
    age = dt.datetime.now(dt.UTC) - dt.datetime.fromisoformat(refresh)
    if age > dt.timedelta(days=7):
        raise RuntimeError("Subscription login snapshot is stale; refresh outside the oracle")
    clean = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": tokens.get("id_token", ""),
            "access_token": access,
            "refresh_token": "",
            "account_id": tokens.get("account_id"),
        },
        "last_refresh": refresh,
    }
    return json.dumps(clean).encode()


def _relay(left: socket.socket, right: socket.socket) -> None:
    """Relay opaque TLS; neither request bodies nor credentials are inspected/logged."""
    try:
        while True:
            readable, _, _ = select.select([left, right], [], [], 60)
            if not readable:
                continue
            for source in readable:
                body = source.recv(65536)
                if not body:
                    return
                (right if source is left else left).sendall(body)
    except (OSError, ValueError):
        return


class _Proxy(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

    def __init__(self, path: Path, *, allow_network: bool):
        self.allow_network = allow_network
        self.events: list[dict[str, Any]] = []
        super().__init__(str(path), _ConnectHandler)


class _ConnectHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        server: _Proxy = self.server  # type: ignore[assignment]
        self.request.settimeout(15)
        header = bytearray()
        try:
            while b"\r\n\r\n" not in header and len(header) < 8192:
                byte = self.request.recv(1)
                if not byte:
                    return
                header.extend(byte)
            first_line = bytes(header).split(b"\r\n", 1)[0].decode("ascii")
            method, target, version = first_line.split(" ")
            allowed = (method == "CONNECT" and version in {"HTTP/1.0", "HTTP/1.1"}
                       and target in {f"{host}:443" for host in ALLOWED_CONNECT_HOSTS}
                       and header.endswith(b"\r\n\r\n"))
            if not allowed or not server.allow_network:
                server.events.append({"action": "denied", "allowlisted_target": allowed})
                self.request.sendall(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n")
                return
            host = target.rsplit(":", 1)[0]
            addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
            if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
                raise OSError("Non-public endpoint rejected")
            upstream = None
            for family, kind, proto, _, address in addresses:
                candidate = socket.socket(family, kind, proto)
                candidate.settimeout(15)
                try:
                    candidate.connect(address)
                    upstream = candidate
                    break
                except OSError:
                    candidate.close()
            if upstream is None:
                raise OSError("Approved endpoint unavailable")
            with upstream:
                server.events.append({"action": "connected", "host": host, "port": 443})
                self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.request.settimeout(None)
                upstream.settimeout(None)
                _relay(self.request, upstream)
        except (OSError, ValueError, UnicodeError):
            server.events.append({"action": "failed"})
            with contextlib.suppress(OSError):
                self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")


# Runs inside the private namespaces. The one mounted Unix socket leads only to
# the explicit CONNECT allowlist; no host IP route or host Unix socket is exposed.
_BOOTSTRAP = r'''
import os, select, socket, socketserver, subprocess, sys, threading
class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as upstream:
            upstream.connect('/isolation/connect.sock')
            try:
                while True:
                    ready, _, _ = select.select([self.request, upstream], [], [], 60)
                    for source in ready:
                        data = source.recv(65536)
                        if not data: return
                        (upstream if source is self.request else self.request).sendall(data)
            except OSError: return
class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
server = Server(('127.0.0.1', 0), Relay)
threading.Thread(target=server.serve_forever, daemon=True).start()
proxy = 'http://127.0.0.1:' + str(server.server_address[1])
os.environ.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, https_proxy=proxy, http_proxy=proxy)
raise SystemExit(subprocess.call(sys.argv[1:], close_fds=True))
'''


@contextlib.contextmanager
def _slot(max_concurrency: int = 2) -> Iterator[None]:
    """Use the shared lock pool; v1 defaults to two, explicitly authorized v2 to four."""
    if type(max_concurrency) is not int or max_concurrency not in {2, 4}:
        raise ValueError("Oracle concurrency must be the registered limit 2 or 4")
    directory = Path(tempfile.gettempdir()) / f"chl-oracle-slots-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if directory.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise RuntimeError("Unsafe oracle concurrency-lock directory")
    held = None
    try:
        for index in range(max_concurrency):
            fd = os.open(directory / f"slot-{index}", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                held = fd
                break
            except BlockingIOError:
                os.close(fd)
        if held is None:
            count = "Two" if max_concurrency == 2 else "Four"
            raise RuntimeError(f"{count} isolated oracle processes are already active")
        yield
    finally:
        if held is not None:
            os.close(held)


class _Sandbox:
    def __init__(self, root: Path, schema: dict[str, Any], auth: bytes | None, *, network: bool, instructions_text: str, effort: str = EFFORT):
        self.root = root
        self.effort = effort
        self.work = root / "work"
        self.work.mkdir(mode=0o700)
        self.output_dir = root / "output"
        self.output_dir.mkdir(mode=0o700)
        self.schema = root / "schema.json"
        self.schema.write_text(json.dumps(schema), encoding="utf-8")
        self.instructions = root / "model-instructions.txt"
        self.instructions.write_text(instructions_text, encoding="utf-8")
        self.catalog = root / "model-catalog.json"
        self.bootstrap = root / "bootstrap.py"
        self.bootstrap.write_text(_BOOTSTRAP, encoding="utf-8")
        self.auth_fd = None
        if auth is not None:
            self.auth_fd = os.memfd_create("oracle-subscription-auth", os.MFD_CLOEXEC)
            os.write(self.auth_fd, auth)
            os.lseek(self.auth_fd, 0, os.SEEK_SET)
        codex = shutil.which("codex")
        if not codex:
            raise RuntimeError("Codex CLI is unavailable")
        self.codex_binary = Path(codex).resolve()
        with self.codex_binary.open("rb") as binary_stream:
            if binary_stream.read(4) != b"\x7fELF":
                raise RuntimeError("This wrapper requires the installed native Codex ELF binary")
        self.proxy = _Proxy(root / "connect.sock", allow_network=False)
        self.thread = threading.Thread(target=self.proxy.serve_forever, daemon=True)
        self.thread.start()
        try:
            self._prepare_catalog()
            self.proxy.allow_network = network
        except BaseException:
            self.close()
            raise

    def _prepare_catalog(self) -> None:
        """Preserve the bundled model descriptor except four local tool capabilities."""
        result = self.execute(["/opt/codex", "debug", "models", "--bundled"])
        if result.returncode:
            raise RuntimeError("Cannot obtain the bundled Codex model catalog offline")
        model = next((row for row in json.loads(result.stdout)["models"] if row["slug"] == MODEL), None)
        if model is None:
            raise RuntimeError("The exact registered model is absent from the bundled catalog")
        if self.effort not in {row["effort"] for row in model["supported_reasoning_levels"]}:
            raise RuntimeError("The exact requested effort is absent from the bundled model descriptor")
        self.catalog_source_sha256 = hashlib.sha256(
            json.dumps(model, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.catalog_diff = {key: {"before": model.get(key), "after": value}
                             for key, value in CATALOG_TOOL_OVERRIDES.items()}
        restricted = {**model, **CATALOG_TOOL_OVERRIDES}
        self.catalog.write_text(
            json.dumps({"models": [restricted]}, sort_keys=True, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )

    def close(self) -> None:
        self.proxy.shutdown()
        self.proxy.server_close()
        self.thread.join(timeout=5)
        if self.auth_fd is not None:
            os.close(self.auth_fd)

    def argv(self, command: list[str]) -> list[str]:
        # Deliberately do not bind /, /etc, /run, host /proc, /home, /var or a repo.
        args = [
            "/usr/bin/bwrap", "--unshare-all", "--unshare-user", "--disable-userns", "--die-with-parent",
            "--new-session", "--cap-drop", "ALL", "--hostname", "oracle-isolated",
            "--clearenv", "--tmpfs", "/", "--proc", "/proc", "--dev", "/dev",
            "--dir", "/usr", "--dir", "/usr/bin", "--ro-bind", "/usr/lib64", "/usr/lib64",
            "--symlink", "usr/lib64", "/lib64",
            "--ro-bind", "/usr/bin/python3", "/usr/bin/python3",
            "--dir", "/opt", "--ro-bind", str(self.codex_binary), "/opt/codex",
            "--dir", "/etc", "--dir", "/etc/ssl", "--dir", "/etc/ssl/certs",
            "--ro-bind", str(Path("/etc/pki/tls/certs/ca-bundle.crt").resolve()),
            "/etc/ssl/certs/ca-certificates.crt",
            "--tmpfs", "/tmp", "--dir", "/home", "--dir", "/home/oracle",
            "--dir", "/home/oracle/.codex", "--dir", "/isolation",
            "--ro-bind", str(self.bootstrap), "/isolation/bootstrap.py",
            "--ro-bind", str(self.schema), "/isolation/output-schema.json",
            "--ro-bind", str(self.instructions), "/isolation/model-instructions.txt",
            "--ro-bind", str(self.root / "connect.sock"), "/isolation/connect.sock",
            "--ro-bind", str(self.work), str(self.work),
            "--bind", str(self.output_dir), "/output", "--chdir", str(self.work),
        ]
        environment = {
            "HOME": "/home/oracle", "CODEX_HOME": "/home/oracle/.codex",
            "PATH": "/opt:/usr/bin", "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
            "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
            "SSL_CERT_DIR": "/etc/ssl/certs", "TMPDIR": "/tmp",
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        }
        for key, value in environment.items():
            args += ["--setenv", key, value]
        if self.auth_fd is not None:
            args += ["--ro-bind-data", str(self.auth_fd), "/home/oracle/.codex/auth.json"]
        if self.catalog.is_file():
            args += ["--ro-bind", str(self.catalog), "/isolation/model-catalog.json"]
        return args + ["--", "/usr/bin/python3", "-I", "/isolation/bootstrap.py", *command]

    def execute(self, command: list[str], *, stdin: str = "", timeout: float = 60) -> subprocess.CompletedProcess[str]:
        if self.auth_fd is not None:
            os.lseek(self.auth_fd, 0, os.SEEK_SET)
        with subprocess.Popen(
            self.argv(command), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, cwd=self.work, env={"PATH": "/usr/bin:/bin"},
            close_fds=True, pass_fds=(() if self.auth_fd is None else (self.auth_fd,)), start_new_session=True,
        ) as proc:
            try:
                stdout, stderr = proc.communicate(stdin, timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                stdout, stderr = proc.communicate()
                # Retain the actual streams, including partial timeout output,
                # for callers which opt into the v2 provenance path.
                exc.stdout, exc.stderr = stdout, stderr
                raise
            except BaseException:
                # bwrap --die-with-parent also tears down its private PID namespace.
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                raise
            return subprocess.CompletedProcess(command, proc.returncode, stdout, stderr)

    def codex_command(self) -> list[str]:
        return [
            "/opt/codex", "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--strict-config", "-s", "read-only", "--skip-git-repo-check",
            "-m", MODEL, "--output-schema", "/isolation/output-schema.json",
            "-C", str(self.work), "--color", "never", "--json",
            "--output-last-message", "/output/final.json", *_config_args(self.effort), "-",
        ]

    def metadata(self) -> dict[str, Any]:
        return {
            "protocol": PROTOCOL_VERSION, "model": MODEL, "effort": self.effort,
            "auth_mode": "none" if self.auth_fd is None else "chatgpt",
            "credential_free": self.auth_fd is None,
            "api_key_allowed": False, "refresh_token_copied": False,
            "os_isolation": "bubblewrap user/pid/net/ipc/uts/cgroup namespaces",
            "fresh_cwd": str(self.work), "fresh_codex_home": "/home/oracle/.codex",
            "codex_binary_sha256": _sha256_file(self.codex_binary),
            "wrapper_sha256": _sha256_file(Path(__file__)),
            "bootstrap_sha256": _sha256_file(self.bootstrap),
            "codex_version": self.execute(["/opt/codex", "--version"]).stdout.strip(),
            "output_schema_sha256": hashlib.sha256(self.schema.read_bytes()).hexdigest(),
            "instructions_sha256": _sha256_file(self.instructions),
            "model_catalog_sha256": _sha256_file(self.catalog),
            "model_catalog_source_descriptor_sha256": self.catalog_source_sha256,
            "model_catalog_tool_overrides": self.catalog_diff,
            "command": self.codex_command(), "argv": self.codex_command(),
            "sandbox_argv": self.argv(self.codex_command()),
            "config": {**CONFIG, "model_reasoning_effort": self.effort},
            "allowed_connect_hosts": sorted(ALLOWED_CONNECT_HOSTS),
            "proxy_events": list(self.proxy.events),
        }


@contextlib.contextmanager
def _sandbox(schema: dict[str, Any], *, timeout: float, network: bool, instructions_text: str, effort: str = EFFORT, credential_free: bool = False) -> Iterator[_Sandbox]:
    if not isinstance(schema, dict):
        raise TypeError("output_schema must be a JSON Schema object")
    if not isinstance(instructions_text, str) or not instructions_text.strip():
        raise ValueError("instructions_text must contain the frozen annotation instructions")
    auth = None if credential_free else _auth_snapshot(timeout)
    with tempfile.TemporaryDirectory(prefix="chl-oracle-call-", dir="/tmp") as directory:
        root = Path(directory).resolve()
        if root.is_relative_to(PROJECT_ROOT):
            raise RuntimeError("Oracle temporary directory must be outside the project")
        sandbox = _Sandbox(root, schema, auth, network=network, instructions_text=instructions_text, effort=effort)
        try:
            yield sandbox
        finally:
            sandbox.close()


def run_isolated_codex(
    prompt: str, output_schema: dict[str, Any], *, instructions_text: str, effort: str = EFFORT, timeout: float = 1800,
    max_concurrency: int = 2, retain_timeout_output: bool = False,
) -> OracleResult:
    """Run one fresh GPT-6 Astra process. No automatic max-to-xhigh fallback occurs."""
    if effort not in {"max", "xhigh"}:
        raise ValueError("The registered oracle permits exact reasoning effort 'max' or 'xhigh'")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be a nonempty string")
    if not isinstance(json.loads(prompt), dict):
        raise TypeError("stdin must contain exactly one item JSON object")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    with _slot(max_concurrency), _sandbox(output_schema, timeout=timeout, network=True, instructions_text=instructions_text, effort=effort) as sandbox:
        timed_out = False
        try:
            result = sandbox.execute(sandbox.codex_command(), stdin=prompt, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            if not retain_timeout_output:
                raise
            timed_out = True
            result = subprocess.CompletedProcess(sandbox.codex_command(), 124, exc.stdout or "", exc.stderr or "")
        output_path = sandbox.output_dir / "final.json"
        output = output_path.read_text(encoding="utf-8") if output_path.is_file() else ""
        metadata = {**sandbox.metadata(), "max_concurrency": max_concurrency, "timed_out": timed_out}
        return OracleResult(result.stdout, result.stderr, output, result.returncode, metadata)


def _render_model_input(sandbox: _Sandbox, prompt: str, instructions_text: str) -> dict[str, Any]:
    # debug prompt-input accepts a positional prompt. This bridge reads the item
    # from stdin inside the namespace before exec, so the host argv has no item.
    bridge = (
        "import os,sys; item=sys.stdin.read(); "
        "os.execv('/opt/codex', ['/opt/codex', *sys.argv[1:], 'debug', 'prompt-input', item])"
    )
    rendered = sandbox.execute(
        ["/usr/bin/python3", "-I", "-c", bridge, *_config_args(sandbox.effort)],
        stdin=prompt, timeout=60,
    )
    if rendered.returncode:
        return {"available": False, "passed": False, "error": rendered.stderr.strip()}
    items = json.loads(rendered.stdout)
    messages = [item for item in items if item.get("type") == "message"]
    checks = {
        "exactly_two_messages": len(items) == len(messages) == 2,
        "no_tool_definitions": all(item.get("type") != "additional_tools" for item in items),
        "instructions_match_file": False,
        "user_item_matches_stdin": False,
        "no_extra_scaffolding": False,
    }
    if len(messages) == 2:
        instructions, user = messages
        def text_of(item: dict[str, Any]) -> str:
            return "".join(part.get("text", "") for part in item.get("content", []))
        checks["instructions_match_file"] = (
            instructions.get("role") == "developer"
            and text_of(instructions) == instructions_text.strip()
            and instructions.get("internal_chat_message_metadata_passthrough", {}).get(
                "content_item_kinds") == ["model.base_instructions"]
        )
        checks["user_item_matches_stdin"] = user.get("role") == "user" and text_of(user) == prompt
        checks["no_extra_scaffolding"] = checks["exactly_two_messages"] and all(
            part.get("type") == "input_text"
            for message in messages for part in message.get("content", [])
        )
    return {
        "available": True, "passed": all(checks.values()), "checks": checks,
        "input_items": items, "raw_input_json": rendered.stdout,
        "raw_input_sha256": hashlib.sha256(rendered.stdout.encode()).hexdigest(),
        "coverage": "Local Codex input list only; server-side/backend additions and response-format schema are not exposed by this command.",
    }


def inspect_model_input(
    prompt: str, output_schema: dict[str, Any], *, instructions_text: str,
    effort: str = EFFORT,
) -> dict[str, Any]:
    """Return observed local assembled input, with zero inference and all egress denied.

    The returned input contains the supplied item; callers must archive it privately.
    It contains no HTTP headers or credentials. This is not a network interception.
    """
    if effort not in {"max", "xhigh"}:
        raise ValueError("The registered oracle permits only max or xhigh")
    if not isinstance(json.loads(prompt), dict):
        raise TypeError("stdin must contain exactly one item JSON object")
    with _slot(), _sandbox(output_schema, timeout=60, network=False, credential_free=True,
                          instructions_text=instructions_text, effort=effort) as sandbox:
        report = _render_model_input(sandbox, prompt, instructions_text)
        report.update(inference_calls=0, network_enabled=False, credential_free=True,
                      metadata=sandbox.metadata())
        return report


def inspect_isolation(
    output_schema: dict[str, Any] | None = None, *, check_transport: bool = False,
    instructions_text: str = "Synthetic isolation instructions. Return only JSON that matches the supplied schema.",
) -> dict[str, Any]:
    """Probe real subprocess boundaries and CLI login/config, with all egress denied."""
    schema = output_schema or {"type": "object", "properties": {}, "additionalProperties": False}
    # Paths are tested inside the sandbox only. No study content is opened on the host.
    forbidden = [
        str(PROJECT_ROOT), str(PROJECT_ROOT / "data"), str(PROJECT_ROOT / "results"),
        str(PROJECT_ROOT / "annotation"), str(PROJECT_ROOT / "annotation" / "labels.jsonl"),
        str(Path.home()), str(Path.home() / ".codex"), str(Path.home() / "AGENTS.md"),
        "/run/user", "/etc/codex", "/var/run", "/proc/1/root" + str(PROJECT_ROOT),
        "/proc/1/root" + str(Path.home()),
    ]
    with _slot(), _sandbox(schema, timeout=60, network=False, instructions_text=instructions_text) as sandbox:
        sentinel = sandbox.root / "host-private-sentinel"
        sentinel.write_text("isolation-sentinel-no-study-data", encoding="utf-8")
        forbidden += [str(sentinel), "/proc/1/root" + str(sentinel)]
        with socket.socket() as host_listener:
            host_listener.bind(("127.0.0.1", 0))
            host_listener.listen()
            host_port = host_listener.getsockname()[1]
            probe = '''
import json, os, pathlib, socket, urllib.parse
paths = json.loads(os.read(0, 65536))
checks = {}
for path in paths['forbidden']:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        checks[path] = True
    else:
        os.close(fd); checks[path] = False
for host, port, name in [('127.0.0.1', paths['host_port'], 'host_loopback'), ('10.0.0.1', 80, 'private_route'), ('1.1.1.1', 443, 'direct_internet')]:
    try:
        connection = socket.create_connection((host, port), timeout=0.3)
    except OSError:
        checks[name] = True
    else:
        connection.close(); checks[name] = False
proxy = urllib.parse.urlsplit(os.environ['HTTPS_PROXY'])
for target in ['127.0.0.1:80', 'localhost:80', 'api.openai.com:443', 'auth.openai.com:443', 'chatgpt.com.evil.invalid:443', 'chatgpt.com:443']:
    with socket.create_connection((proxy.hostname, proxy.port), timeout=2) as connection:
        connection.sendall(('CONNECT '+target+' HTTP/1.1\\r\\nHost: '+target+'\\r\\n\\r\\n').encode())
        checks['proxy_denies_'+target] = connection.recv(1024).startswith(b'HTTP/1.1 403')
checks['empty_cwd'] = list(pathlib.Path.cwd().iterdir()) == []
checks['schema_readable'] = isinstance(json.loads(pathlib.Path('/isolation/output-schema.json').read_text()), dict)
checks['instructions_readable'] = pathlib.Path('/isolation/model-instructions.txt').is_file()
checks['runtime_readable'] = pathlib.Path('/opt/codex').is_file()
checks['no_inherited_secrets'] = not any(k.startswith(('OPENAI_', 'CLAUDE_', 'CODEX_SESSION', 'CODEX_THREAD', 'CODEX_COMPANION')) for k in os.environ)
checks['no_host_fds'] = all(not any(part in os.readlink(p) for part in [paths['project'], paths['sentinel'], '/var/home/antonio', 'oracle-subscription-auth']) for p in pathlib.Path('/proc/self/fd').iterdir() if p.exists())
checks['private_pid_namespace'] = len([p for p in pathlib.Path('/proc').iterdir() if p.name.isdigit()]) <= 5
print(json.dumps(checks, sort_keys=True))
'''
            result = sandbox.execute(
                ["/usr/bin/python3", "-I", "-c", probe],
                stdin=json.dumps({"forbidden": forbidden, "host_port": host_port,
                                  "project": str(PROJECT_ROOT), "sentinel": str(sentinel)}),
            )
        if result.returncode:
            raise RuntimeError(f"Isolation probe could not execute: {result.stderr.strip()}")
        checks = json.loads(result.stdout)
        login = sandbox.execute(["/opt/codex", *_config_args(), "login", "status"])
        checks["chatgpt_login"] = login.returncode == 0 and "ChatGPT" in login.stderr + login.stdout
        features = sandbox.execute(["/opt/codex", *_config_args(), "features", "list"])
        checks["cli_accepts_config"] = features.returncode == 0
        feature_lines = {line.split()[0]: line.split()[-1] for line in features.stdout.splitlines() if line.strip()}
        checks["features_disabled"] = all(feature_lines.get(name) == "false" for name in DISABLED_FEATURES)
        # A missing schema stops exec before model/session startup. All outbound
        # traffic is denied for this subprocess even if a CLI update changes order.
        invalid = sandbox.codex_command()
        invalid[invalid.index("--output-schema") + 1] = "/intentionally-missing-schema.json"
        previous_events = len(sandbox.proxy.events)
        validation = sandbox.execute(invalid, stdin="Synthetic configuration validation only.", timeout=30)
        checks["exact_exec_config_accepted"] = (
            validation.returncode != 0
            and "Failed to read output schema file" in validation.stderr
            and "intentionally-missing-schema.json" in validation.stderr
            and len(sandbox.proxy.events) == previous_events
        )
        report = {"passed": all(checks.values()), "checks": checks, "inference_calls": 0,
                  "network_enabled": False, "metadata": sandbox.metadata()}
        if not checks["cli_accepts_config"]:
            report["config_error"] = features.stderr
    rendered = inspect_model_input('{"synthetic_item":"isolation_probe"}', schema,
                                   instructions_text=instructions_text)
    report["local_model_input"] = rendered
    report["checks"]["local_model_input_verified"] = rendered["passed"]
    report["checks"]["local_model_input_credential_free"] = rendered["credential_free"]
    if check_transport:
        # HTTPS HEAD proves the approved TLS path, without any inference endpoint,
        # authentication header, prompt, model, or study data in the request.
        with _sandbox(schema, timeout=60, network=True, credential_free=True, instructions_text=instructions_text) as transport:
            result = transport.execute(["/usr/bin/python3", "-I", "-c", '''
import http.client, json, os, ssl, urllib.parse
proxy = urllib.parse.urlsplit(os.environ['HTTPS_PROXY'])
connection = http.client.HTTPSConnection(proxy.hostname, proxy.port, context=ssl.create_default_context(), timeout=20)
connection.set_tunnel('chatgpt.com', 443)
connection.request('HEAD', '/')
response = connection.getresponse()
print(json.dumps({'tls_verified': True, 'http_status': response.status}))
connection.close()
'''], timeout=30)
            report["transport"] = json.loads(result.stdout) if result.returncode == 0 else {
                "tls_verified": False, "error": result.stderr.strip(),
            }
            report["checks"]["approved_https_transport"] = report["transport"]["tls_verified"]
            report["transport_proxy_events"] = list(transport.proxy.events)
    report["passed"] = all(report["checks"].values())
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inspect", action="store_true", help="run no-inference isolation probes")
    parser.add_argument("--check-transport", action="store_true", help="also perform unauthenticated HTTPS HEAD to chatgpt.com")
    args = parser.parse_args()
    if not args.inspect:
        parser.error("Only --inspect is exposed on the CLI; real calls require the Python interface")
    report = inspect_isolation(check_transport=args.check_transport)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
