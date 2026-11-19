"""Desktop update check and Windows executable replacement.

The check, cache and footer states follow the Blender and Substance Painter
bridges. Those hosts replace a plugin folder. This app replaces
``Tripo-Echo3D-Bridge.exe`` after the process exits, then starts it again.

A published package is a zip with exactly two entries:

- ``Tripo-Echo3D-Bridge.exe``
- ``version.txt`` containing one assignment, ``__version__ = "x.y.z"``

The zip SHA-256 is the manifest digest. ``version.txt`` is read as a literal,
never executed. ``TRIPO_UPDATE_CHECK=0`` disables checks.
``TRIPO_UPDATE_URL`` may point at ``http://127.0.0.1`` for a local manifest.
"""
from pathlib import Path, PurePosixPath
import ast
import hashlib
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import zipfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


PLUGIN_ID = "echo3d-bridge"
CHANNEL = "intl"  # A domestic package build must replace this literal.
INTL_BASE = "https://tripo-public.tripo3d.ai/plugins/echo3d-bridge/"
CN_BASE = "https://assets.cdn.tripo3d.com/fe-tripo-studio/plugins/echo3d-bridge/"
USER_AGENT = "Tripo-Echo3D-Update/1"
TIMEOUT = 4
MAX_BYTES = 65536
TTL = 24 * 60 * 60
EXE_NAME = "Tripo-Echo3D-Bridge.exe"
VERSION_NAME = "version.txt"
LATEST_NAME = "Tripo3d_echo3d_Bridge-latest.zip"
ALLOWED_HOSTS = frozenset({"tripo-public.tripo3d.ai", "tripo-public.tripo3d.com",
                           "assets.cdn.tripo3d.com", "tripo-cn-files.tripo3d.com"})
MAX_DOWNLOAD = 200 * 1024 * 1024
STAGE_PREFIX = ".tripo-stage-"


def version(value):
    if not isinstance(value, str) or not re.fullmatch(r"[vV]?\d+[._]\d+[._]\d+", value):
        raise ValueError("invalid stable version")
    return tuple(int(part) for part in value.lstrip("vV").replace("_", ".").split("."))


def version_text(value):
    return ".".join(map(str, version(value)))


def channel_base():
    if CHANNEL == "cn":
        return CN_BASE
    if CHANNEL != "intl":
        raise ValueError("Unknown distribution channel")
    return INTL_BASE


def _local_override():
    return urlsplit(os.environ.get("TRIPO_UPDATE_URL", "")).hostname == "127.0.0.1"


def safe_url(value, local=False):
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ValueError("invalid URL")
    parts = urlsplit(value)
    if parts.username or parts.password or not parts.hostname:
        raise ValueError("invalid URL")
    if parts.scheme != "https" and not (local and parts.scheme == "http" and parts.hostname == "127.0.0.1"):
        raise ValueError("HTTPS required")
    return value


class SafeRedirect(HTTPRedirectHandler):
    def __init__(self, local):
        self.local = local

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe_url(newurl, self.local)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def clean_notes(value):
    if not isinstance(value, str):
        return ""
    return "".join(char for char in value[:2000] if char == "\n" or not unicodedata.category(char).startswith("C"))


def resolve(manifest, current):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1 or manifest.get("id") != PLUGIN_ID:
        raise ValueError("invalid manifest identity/schema")
    latest = manifest["version"]
    latest_v, current_v = version(latest), version(current)
    package = manifest["packages"]["default"]
    url = safe_url(package["url"], _local_override())
    digest = package.get("sha256", "")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise ValueError("invalid package SHA-256")
    result = {"available": latest_v > current_v, "current": current, "latest": version_text(latest),
              "url": url, "sha256": digest.lower(), "channel": manifest.get("channel"),
              "auto_install": manifest.get("channel") == CHANNEL,
              "notes": "", "feature_version": "", "release_url": ""}
    feature = manifest.get("feature_release")
    if result["available"] and isinstance(feature, dict):
        try:
            feature_version = version(feature.get("version"))
            if current_v[:2] < feature_version[:2] == latest_v[:2] and feature_version <= latest_v:
                result["notes"] = clean_notes(feature.get("notes"))
                result["feature_version"] = feature["version"]
                if feature.get("release_url"):
                    result["release_url"] = safe_url(feature["release_url"])
        except (ValueError, TypeError):
            result.update(notes="", feature_version="", release_url="")
    return result


def latest_package_url():
    return channel_base() + LATEST_NAME


def sources():
    override = os.environ.get("TRIPO_UPDATE_URL")
    return [safe_url(override, True)] if override else [channel_base() + "manifest.json"]


def fetch(url, local=False, method="GET"):
    safe_url(url, local)
    request = Request(url, method=method, headers={"User-Agent": USER_AGENT})
    with build_opener(SafeRedirect(local)).open(request, timeout=TIMEOUT) as response:
        if method == "HEAD":
            published = response.headers.get("x-amz-meta-version") or response.headers.get("x-oss-meta-version")
            digest = response.headers.get("x-amz-meta-sha256") or response.headers.get("x-oss-meta-sha256") or ""
            if not published:
                raise ValueError("missing package version")
            return published, digest
        raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("manifest too large")
        return json.loads(raw)


def check(current, cache_dir, urls=None, now=None, force=False):
    """Run on a worker. Cache the manifest, not the derived button state."""
    if os.environ.get("TRIPO_UPDATE_CHECK") == "0":
        return {"disabled": True}
    urls = sources() if urls is None else urls
    now = time.time() if now is None else now
    version(current)
    key = hashlib.sha256(json.dumps([PLUGIN_ID, current, urls]).encode()).hexdigest()
    cache = Path(cache_dir) / (key + ".json")

    def save(entry):
        temporary = None
        try:
            cache.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache.parent, delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(dict(entry, time=now), handle)
            temporary.replace(cache)
        except OSError:
            pass
        finally:
            if temporary:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def legacy(latest, url, digest=""):
        verified = digest.lower() if isinstance(digest, str) and re.fullmatch(r"[0-9a-fA-F]{64}", digest) else ""
        return {"available": version(latest) > version(current), "current": current,
                "latest": version_text(latest), "url": safe_url(url), "sha256": verified,
                "channel": CHANNEL if verified else "", "auto_install": bool(verified),
                "notes": "", "feature_version": "", "release_url": ""}

    try:
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if not force and 0 <= now - saved["time"] < TTL:
            if "manifest" in saved:
                result = resolve(saved["manifest"], current)
            elif "legacy" in saved:
                result = legacy(**saved["legacy"])
            else:
                result = {"error": str(saved["error"])[:200]}
            result["cached"] = True
            return result
    except (OSError, ValueError, KeyError, TypeError):
        pass
    errors = []
    for url in urls:
        try:
            manifest = fetch(url, local=bool(os.environ.get("TRIPO_UPDATE_URL")))
            result = resolve(manifest, current)
            save({"manifest": manifest})
            return result
        except Exception as exc:
            errors.append(type(exc).__name__)
    # The published object is the latest zip. A missing manifest must not hide that package.
    if not os.environ.get("TRIPO_UPDATE_URL") and set(urls) == set(sources()):
        try:
            url = latest_package_url()
            published, digest = fetch(url, method="HEAD")
            result = legacy(published, url, digest)
            save({"legacy": {"latest": published, "url": url, "digest": digest}})
            return result
        except Exception as exc:
            errors.append(type(exc).__name__)
    error = "all update sources failed: " + ", ".join(errors)
    save({"error": error})
    return {"error": error}


class Cancelled(Exception):
    pass


def ensure_running(cancel):
    if cancel.is_set():
        raise Cancelled("Update cancelled")


def _literals(source, name):
    values = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            values.append(ast.literal_eval(node.value))
    return values


def _unsafe_entry(name, member):
    path = PurePosixPath(name)
    parts = name.rstrip("/").split("/")
    if (member.is_dir() or path.is_absolute() or "\\" in name or len(parts) != 1
            or parts[0] not in (EXE_NAME, VERSION_NAME)
            or stat.S_ISLNK(member.external_attr >> 16) or member.flag_bits & 1):
        return True
    return False


def archive_info(archive, expected):
    """Inspect every name before writing anything. Never execute the package."""
    with zipfile.ZipFile(archive) as package:
        members = package.infolist()
        if len(members) > 10 or sum(member.file_size for member in members) > MAX_DOWNLOAD:
            raise ValueError("Package exceeds extraction limits")
        seen = set()
        for member in members:
            if _unsafe_entry(member.filename, member):
                raise ValueError("Unsafe ZIP entry")
            key = member.filename.casefold()
            if key in seen:
                raise ValueError("Duplicate ZIP entry")
            seen.add(key)
        names = {member.filename for member in members}
        if names != {EXE_NAME, VERSION_NAME}:
            raise ValueError("Not a Tripo Echo3D Bridge package")
        declared = _literals(package.read(VERSION_NAME).decode("utf-8-sig"), "__version__")
        if len(declared) != 1 or version(declared[0]) != version(expected):
            raise ValueError("Package version does not match manifest")
        return members


def cleanup(stage):
    root = Path(stage["root"]).resolve()
    parent = Path(stage["parent"]).resolve()
    if root.parent != parent or not root.name.startswith(STAGE_PREFIX):
        raise ValueError("Invalid staging cleanup path")
    if root.exists() and not root.is_symlink():
        shutil.rmtree(root)


def prepare(update, parent, cancel, progress=lambda done, total: None):
    if update.get("channel") != CHANNEL or not update.get("auto_install"):
        raise ValueError("Update channel is not verified for automatic installation")
    if version(update["latest"]) <= version(update["current"]):
        raise ValueError("Update must be newer than the running app")
    digest = update.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("Missing package SHA-256")
    local = _local_override()
    url = safe_url(update["url"], local)
    parsed = urlsplit(url)
    if parsed.hostname not in ALLOWED_HOSTS and not (local and parsed.hostname == "127.0.0.1"):
        raise ValueError("Package must be hosted by the Tripo release CDN")
    if not parsed.path.lower().endswith(".zip"):
        raise ValueError("Update package must be a ZIP")
    parent = Path(parent).resolve()
    parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=STAGE_PREFIX, dir=parent))
    stage = {"root": str(root), "parent": str(parent), "version": update["latest"]}
    try:
        archive = root / "download.zip"
        checksum = hashlib.sha256()
        start = time.monotonic()
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with build_opener(SafeRedirect(local)).open(request, timeout=10) as response, archive.open("wb") as output:
            total = int(response.headers.get("Content-Length", 0))
            if total > MAX_DOWNLOAD:
                raise ValueError("Download is too large")
            count = 0
            while True:
                ensure_running(cancel)
                if time.monotonic() - start > 180:
                    raise TimeoutError("Download deadline exceeded")
                data = response.read(65536)
                if not data:
                    break
                count += len(data)
                if count > MAX_DOWNLOAD:
                    raise ValueError("Download is too large")
                output.write(data)
                checksum.update(data)
                progress(count, total)
        if checksum.hexdigest() != digest:
            raise ValueError("Package SHA-256 verification failed")
        members = archive_info(archive, update["latest"])
        with zipfile.ZipFile(archive) as package:
            for member in members:
                ensure_running(cancel)
                destination = root / member.filename
                with package.open(member) as source, destination.open("xb") as output:
                    shutil.copyfileobj(source, output)
        exe = root / EXE_NAME
        if not exe.is_file() or exe.is_symlink() or exe.stat().st_size <= 0:
            raise ValueError("Packaged executable is missing")
        stage["exe"] = str(exe)
        return stage
    except BaseException:
        cleanup(stage)
        raise


def packaged_executable():
    """The running PyInstaller executable. A source checkout is not a target."""
    if not getattr(sys, "frozen", False):
        return None
    path = Path(sys.executable).resolve()
    if path.is_symlink() or path.suffix.lower() != ".exe" or not path.is_file():
        return None
    return path


def _ps_quote(value):
    text = str(value)
    if any(char in text for char in "\r\n\x00"):
        raise ValueError("invalid path")
    return "'" + text.replace("'", "''") + "'"


def restart_script(process_id, new_exe, dest):
    """Wait until this process exits, then replace the executable and start it."""
    return "\n".join([
        "$ErrorActionPreference = 'Stop'",
        "$processId = %d" % int(process_id),
        "$newExe = %s" % _ps_quote(new_exe),
        "$dest = %s" % _ps_quote(dest),
        "Wait-Process -Id $processId -ErrorAction SilentlyContinue",
        "Start-Sleep -Milliseconds 800",
        "try { Copy-Item -LiteralPath $dest -Destination ($dest + '.previous') -Force } catch {}",
        "$moved = $false",
        "for ($attempt = 0; $attempt -lt 30; $attempt++) {",
        "    try {",
        "        Move-Item -LiteralPath $newExe -Destination $dest -Force",
        "        $moved = $true",
        "        break",
        "    } catch { Start-Sleep -Milliseconds 500 }",
        "}",
        "if (-not $moved) { exit 1 }",
        "Start-Process -FilePath $dest",
        "",
    ])


def schedule_restart(new_exe, dest):
    new_exe, dest = Path(new_exe), Path(dest)
    if new_exe.is_symlink() or dest.is_symlink() or not new_exe.is_file() or not dest.is_file():
        raise ValueError("invalid install path")
    if new_exe.name != EXE_NAME or dest.suffix.lower() != ".exe":
        raise ValueError("invalid install path")
    script = new_exe.with_name("replace.ps1")
    script.write_text(restart_script(os.getpid(), new_exe, dest), encoding="utf-8-sig")
    options = {"close_fds": True, "stdin": subprocess.DEVNULL,
               "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if sys.platform == "win32":
        options["creationflags"] = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File", str(script)],
        **options,
    )


class UpdateDisplay:
    def __init__(self, label, icon, ready, busy, spinning, hint):
        self.label, self.icon, self.ready, self.busy, self.spinning, self.hint = (
            label, icon, ready, busy, spinning, hint)


def update_display(*, status, state, notice, update_ready, transferring=False, blocked="", can_install=False):
    """Footer states shared with Blender and Substance Painter."""
    phase = state.get("phase")
    checking, disabled = status.get("checking"), status.get("disabled")
    busy = bool(phase in ("downloading", "installed") or checking or disabled or transferring or blocked)
    ready = bool(update_ready and notice.get("auto_install") and can_install and not busy)
    if phase == "installed":
        label = "Restarting"
    elif phase == "downloading":
        label = "Updating {progress}%".format(progress=round(state.get("progress", 0) * 100))
    elif checking:
        label = "Checking for updates…"
    elif disabled:
        label = "Update checks are disabled"
    elif transferring:
        label = "Finish the current transfer before updating"
    elif blocked:
        label = blocked
    elif phase == "failed":
        label = "Update failed. Click to retry"
    elif notice.get("error"):
        label = "Check failed. Click to retry"
    elif notice.get("available"):
        template = "Install v{version}" if ready else "Update available: v{version}"
        label = template.format(version=notice["latest"])
    else:
        label = "Check for updates"
    hint = "New version" if notice.get("available") and phase not in ("downloading", "installed") else ""
    return UpdateDisplay(label, "download" if ready else "refresh", ready, busy, bool(checking), hint)


class Updater:
    """Worker threads plus main-thread ``poll``, same split as the DCC plugins."""

    def __init__(self, current, cache_dir, *, is_busy, on_restart):
        self.current = current
        self.cache_dir = Path(cache_dir)
        self._is_busy = is_busy
        self._on_restart = on_restart
        self.status = {"checking": False, "checked": False, "manual": False, "disabled": False}
        self.notice = {}
        self.state = {}
        self._messages = queue.Queue()
        self._handoff = threading.Lock()
        self._check_token = None
        self._install_token = None
        self._progress = 0.0
        self._handed_off = False

    @property
    def installed(self):
        return self.state.get("phase") == "installed"

    @property
    def update_ready(self):
        """A manual check found an update. The next click may install it."""
        return bool(self.status["manual"] and self.status["checked"] and not self.status["checking"]
                    and self.notice.get("available") and not self.notice.get("error")
                    and self.state.get("phase") != "failed")

    def start_check(self, *, manual):
        self._cancel_check()
        if os.environ.get("TRIPO_UPDATE_CHECK") == "0":
            self.status.update(checking=False, checked=True, disabled=True, manual=manual)
            return
        self.status.update(checking=True, checked=False, disabled=False, manual=manual)
        token = threading.Event()
        self._check_token = token

        def worker():
            try:
                result = check(self.current, self.cache_dir, force=manual)
            except Exception as exc:
                result = {"error": str(exc)}
            if not token.is_set():
                self._messages.put(("checked", token, result))

        threading.Thread(target=worker, name="TripoUpdateCheck", daemon=True).start()

    def request_check(self):
        if self.status["checking"] or self.state.get("phase") in ("downloading", "installed"):
            return False
        self.state.clear()
        self.start_check(manual=True)
        return True

    def require_idle(self):
        if self._is_busy():
            raise ValueError("Disconnect Tripo Studio and finish the current transfer before updating")

    def start_install(self):
        if self.state.get("phase") in ("downloading", "installed"):
            return False
        try:
            if packaged_executable() is None:
                raise ValueError("Automatic install replaces the packaged Windows app and restarts it.")
            self.require_idle()
        except Exception as exc:
            self.state.update(phase="failed", error=str(exc))
            return False
        token = threading.Event()
        self._install_token = token
        self._progress = 0.0
        self.state.clear()
        self.state.update(phase="downloading", progress=0.0)

        def progress(done, total):
            with self._handoff:
                if not token.is_set():
                    self._progress = done / total if total else 0.0

        def worker():
            stage = None
            try:
                update = check(self.current, self.cache_dir, force=True)
                if not update.get("available") or not update.get("auto_install"):
                    raise ValueError(update.get("error") or "No installable release is available")
                stage = prepare(update, self.cache_dir, token, progress)
                with self._handoff:
                    if not token.is_set():
                        self._messages.put(("ready", token, (stage, update)))
                        stage = None
            except Exception as exc:
                if not token.is_set():
                    self._messages.put(("failed", token, str(exc)))
            finally:
                if stage:
                    cleanup(stage)

        threading.Thread(target=worker, name="TripoUpdateInstall", daemon=True).start()
        return True

    def poll(self):
        if self.state.get("phase") == "downloading":
            self.state["progress"] = self._progress
        while True:
            try:
                kind, token, payload = self._messages.get_nowait()
            except queue.Empty:
                return
            if kind == "checked" and token is self._check_token:
                self._finish_check(payload)
            elif kind == "failed" and token is self._install_token:
                self.state.update(phase="failed", error=payload)
            elif kind == "ready" and token is self._install_token:
                self._commit(*payload)
            elif kind == "ready":
                self._discard(payload[0])

    def _finish_check(self, result):
        self._check_token = None
        self.notice.clear()
        self.notice.update(result)
        self.status.update(checking=False, checked=True)

    def _commit(self, stage, update):
        self.notice.update(update)
        try:
            self.require_idle()
            exe = packaged_executable()
            if exe is None:
                raise ValueError("Automatic install replaces the packaged Windows app and restarts it.")
            schedule_restart(Path(stage["exe"]), exe)
            self._handed_off = True
            self.state.update(phase="installed", latest=update["latest"], progress=1.0)
            self._on_restart()
        except Exception as exc:
            self.state.update(phase="failed", error=str(exc))
            if not self._handed_off:
                self._discard(stage)

    @staticmethod
    def _discard(stage):
        try:
            cleanup(stage)
        except (OSError, ValueError):
            pass

    def _cancel_check(self):
        if self._check_token is not None:
            self._check_token.set()
        self._check_token = None
        self.notice.clear()
        self.status.update(checking=False, checked=False, disabled=False, manual=False)

    def shutdown(self):
        self._cancel_check()
        with self._handoff:
            if self._install_token is not None:
                self._install_token.set()
            while True:
                try:
                    kind, _token, payload = self._messages.get_nowait()
                except queue.Empty:
                    break
                if kind == "ready" and not self._handed_off:
                    self._discard(payload[0])
        self._install_token = None
        if not self._handed_off:
            self.state.clear()
