from __future__ import annotations

import datetime as dt
import json
import os
import queue
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, render_template, request


app = Flask(__name__)


LOG_LIMIT = 500
JOB_LIMIT = 60
WORKER_COUNT = 1
DEFAULT_OUTPUT_DIR = "~/Downloads/yt-dlp-ui"
DEFAULT_VIDEO_FORMAT = "bestvideo*+bestaudio/best"
DEFAULT_RETRY_MAX = 2
MAX_RETRY_MAX = 10
DEFAULT_RETRY_STRATEGY = "fixed"
DEFAULT_RETRY_DELAY_SECONDS = 3
MAX_RETRY_DELAY_SECONDS = 120
DEFAULT_RETRY_MAX_DELAY_SECONDS = 60
MAX_RETRY_MAX_DELAY_SECONDS = 600
DEFAULT_AUTO_OPEN_OUTPUT = False
MAX_PROXY_POOL_SIZE = 20
RETRY_STRATEGIES = {"fixed", "exponential"}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")


@dataclass
class DownloadJob:
    id: str
    url: str
    output_dir: str
    audio_only: bool
    audio_format: str
    format_id: str
    extra_args: str
    playlist: bool
    auto_open_output: bool = DEFAULT_AUTO_OPEN_OUTPUT
    retry_max: int = DEFAULT_RETRY_MAX
    retry_strategy: str = DEFAULT_RETRY_STRATEGY
    retry_delay_seconds: int = DEFAULT_RETRY_DELAY_SECONDS
    retry_max_delay_seconds: int = DEFAULT_RETRY_MAX_DELAY_SECONDS
    proxy_pool: list[str] = field(default_factory=list)
    proxy_index: int = 0
    active_proxy: str | None = None
    status: str = "queued"
    progress: float = 0.0
    message: str = "等待开始"
    output_file: str | None = None
    created_at: str = field(default_factory=lambda: dt.datetime.now(dt.UTC).isoformat())
    updated_at: str = field(default_factory=lambda: dt.datetime.now(dt.UTC).isoformat())
    logs: list[str] = field(default_factory=list)
    cancel_requested: bool = False
    retry_count: int = 0

    def add_log(self, line: str) -> None:
        text = line.rstrip("\n")
        if not text:
            return
        self.logs.append(text)
        if len(self.logs) > LOG_LIMIT:
            self.logs = self.logs[-LOG_LIMIT:]
        self.updated_at = dt.datetime.now(dt.UTC).isoformat()


def now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def strip_ansi(text: str) -> str:
    return ANSI_RE.sub("", text)


def fmt_duration(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "-"
    total = int(seconds)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def fmt_size(size: Any) -> str:
    if not isinstance(size, (int, float)) or size <= 0:
        return ""
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    idx = 0
    while value >= 1024 and idx < len(units) - 1:
        value /= 1024
        idx += 1
    return f"{value:.1f}{units[idx]}"


def clamp_retry_max(value: Any) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return DEFAULT_RETRY_MAX
    return max(0, min(parsed, MAX_RETRY_MAX))


def clamp_retry_delay_seconds(value: Any) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return DEFAULT_RETRY_DELAY_SECONDS
    return max(1, min(parsed, MAX_RETRY_DELAY_SECONDS))


def clamp_retry_max_delay_seconds(value: Any) -> int:
    try:
        parsed = int(str(value).strip())
    except Exception:
        return DEFAULT_RETRY_MAX_DELAY_SECONDS
    return max(1, min(parsed, MAX_RETRY_MAX_DELAY_SECONDS))


def normalize_retry_strategy(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in RETRY_STRATEGIES else DEFAULT_RETRY_STRATEGY


def retry_strategy_label(value: str) -> str:
    return "指数退避" if value == "exponential" else "固定间隔"


def normalize_proxy_pool(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [line.strip() for line in value.splitlines()]
    elif isinstance(value, list):
        candidates = [str(item).strip() for item in value]
    else:
        return []

    deduped: list[str] = []
    seen: set[str] = set()
    for proxy in candidates:
        if not proxy or proxy in seen:
            continue
        seen.add(proxy)
        deduped.append(proxy)
        if len(deduped) >= MAX_PROXY_POOL_SIZE:
            break
    return deduped


def current_proxy(job: DownloadJob) -> tuple[str | None, int, int]:
    total = len(job.proxy_pool)
    if total <= 0:
        return None, 0, 0
    index = job.proxy_index % total
    return job.proxy_pool[index], index, total


def split_extra_args(extra_args: str) -> list[str]:
    text = extra_args.strip()
    if not text:
        return []
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def strip_proxy_flags(tokens: list[str]) -> list[str]:
    cleaned: list[str] = []
    skip_next = False
    for token in tokens:
        if skip_next:
            skip_next = False
            continue
        if token == "--proxy":
            skip_next = True
            continue
        if token.startswith("--proxy="):
            continue
        cleaned.append(token)
    return cleaned


JOBS: dict[str, DownloadJob] = {}
PROCESSES: dict[str, subprocess.Popen[str]] = {}
JOB_QUEUE: queue.Queue[str] = queue.Queue()
JOBS_LOCK = threading.Lock()
WORKER_LOCK = threading.Lock()
WORKER_STARTED = False


def get_yt_dlp_cmd() -> list[str] | None:
    binary = shutil.which("yt-dlp")
    if binary:
        return [binary]

    try:
        import yt_dlp  # noqa: F401

        return [sys.executable, "-m", "yt_dlp"]
    except Exception:
        return None


def update_job(job_id: str, **kwargs: Any) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        for key, value in kwargs.items():
            setattr(job, key, value)
        job.updated_at = now_iso()


def snapshot_job(job_id: str) -> dict[str, Any] | None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return None
        return asdict(job)


def list_jobs_snapshot() -> list[dict[str, Any]]:
    with JOBS_LOCK:
        jobs = [asdict(job) for job in JOBS.values()]
    return sorted(jobs, key=lambda item: item["created_at"], reverse=True)


def append_log(job_id: str, line: str) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.add_log(strip_ansi(line))


def prune_jobs_locked() -> None:
    if len(JOBS) <= JOB_LIMIT:
        return

    done_like = [job for job in JOBS.values() if job.status in {"done", "failed", "canceled"}]
    active = [job for job in JOBS.values() if job.status not in {"done", "failed", "canceled"}]
    ordered = sorted(done_like, key=lambda item: item.created_at) + sorted(active, key=lambda item: item.created_at)

    while len(JOBS) > JOB_LIMIT and ordered:
        victim = ordered.pop(0)
        JOBS.pop(victim.id, None)
        PROCESSES.pop(victim.id, None)


def register_process(job_id: str, process: subprocess.Popen[str]) -> None:
    with JOBS_LOCK:
        PROCESSES[job_id] = process


def remove_process(job_id: str) -> None:
    with JOBS_LOCK:
        PROCESSES.pop(job_id, None)


def get_process(job_id: str) -> subprocess.Popen[str] | None:
    with JOBS_LOCK:
        return PROCESSES.get(job_id)


def requeue_job_after_delay(job_id: str, delay_seconds: int) -> None:
    time.sleep(delay_seconds)
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        if job.cancel_requested or job.status != "retrying":
            return
        job.status = "queued"
        job.message = f"准备第 {job.retry_count + 1} 次尝试"
        job.updated_at = now_iso()
    JOB_QUEUE.put(job_id)


def compute_retry_delay_seconds(job: DownloadJob, attempt: int) -> int:
    base_delay = max(1, int(job.retry_delay_seconds))
    max_delay = max(base_delay, int(job.retry_max_delay_seconds))
    if job.retry_strategy == "exponential":
        return min(base_delay * (2 ** max(0, attempt - 1)), max_delay)
    return base_delay


def schedule_retry(job_id: str, reason: str) -> bool:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return False
        if job.cancel_requested:
            return False
        if job.retry_count >= job.retry_max:
            return False

        job.retry_count += 1
        attempt = job.retry_count
        retry_max = job.retry_max
        retry_strategy = job.retry_strategy
        delay_seconds = compute_retry_delay_seconds(job, attempt)
        next_proxy, next_proxy_index, proxy_total = current_proxy(job)
        if proxy_total > 0:
            job.proxy_index = (job.proxy_index + 1) % proxy_total
            next_proxy, next_proxy_index, proxy_total = current_proxy(job)
        job.status = "retrying"
        job.progress = 0.0
        job.output_file = None
        job.active_proxy = None
        proxy_note = ""
        if next_proxy:
            proxy_note = f"，下次切换代理 {next_proxy_index + 1}/{proxy_total}"
        job.message = (
            f"{reason}，{delay_seconds}s 后自动重试 "
            f"({attempt}/{retry_max}, {retry_strategy_label(retry_strategy)}{proxy_note})"
        )
        job.updated_at = now_iso()

    proxy_log = ""
    if next_proxy:
        proxy_log = f" | next proxy {next_proxy_index + 1}/{proxy_total}: {next_proxy}"

    append_log(
        job_id,
        f"[retry] {reason} | {delay_seconds}s 后重试 ({attempt}/{retry_max}, {retry_strategy}){proxy_log}",
    )

    thread = threading.Thread(
        target=requeue_job_after_delay,
        args=(job_id, delay_seconds),
        name=f"retry-{job_id}",
        daemon=True,
    )
    thread.start()
    return True


def build_file_manager_command(path: Path) -> list[str]:
    path_text = str(path)

    if os.name == "nt":
        return ["explorer", path_text]

    if sys.platform == "darwin":
        opener = shutil.which("open")
        if not opener:
            raise RuntimeError("未找到 open 命令")
        return [opener, path_text]

    opener = shutil.which("xdg-open")
    if opener:
        return [opener, path_text]

    gio = shutil.which("gio")
    if gio:
        return [gio, "open", path_text]

    raise RuntimeError("未找到可用文件管理器命令（xdg-open/gio/open）")


def resolve_open_target(output_dir_text: str, output_file: str | None) -> Path:
    output_dir = Path(output_dir_text).expanduser().resolve()

    if output_file:
        output_path = Path(output_file).expanduser()
        if not output_path.is_absolute():
            output_path = output_dir / output_path
        output_path = output_path.resolve()
        target = output_path if output_path.is_dir() else output_path.parent
        if target.exists():
            return target

    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def open_output_path(output_dir_text: str, output_file: str | None) -> str:
    target = resolve_open_target(output_dir_text, output_file)
    command = build_file_manager_command(target)
    subprocess.Popen(  # noqa: S603
        command,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return str(target)


def download_worker() -> None:
    while True:
        job_id = JOB_QUEUE.get()
        try:
            run_download(job_id)
        except Exception as exc:
            append_log(job_id, f"[worker] unexpected error: {exc}")
            update_job(job_id, status="failed", message=f"任务异常终止：{exc}")
        finally:
            JOB_QUEUE.task_done()


def ensure_worker_started() -> None:
    global WORKER_STARTED
    with WORKER_LOCK:
        if WORKER_STARTED:
            return
        for idx in range(WORKER_COUNT):
            thread = threading.Thread(
                target=download_worker,
                name=f"yt-dlp-worker-{idx + 1}",
                daemon=True,
            )
            thread.start()
        WORKER_STARTED = True


def build_command(job: DownloadJob) -> list[str]:
    yt_dlp_cmd = get_yt_dlp_cmd()
    if not yt_dlp_cmd:
        raise RuntimeError("未检测到 yt-dlp，请先安装（python3 -m pip install -U yt-dlp）")

    output_dir = Path(job.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / "%(title)s [%(id)s].%(ext)s")

    command = [
        *yt_dlp_cmd,
        "--newline",
        "-o",
        output_template,
    ]

    if not job.playlist:
        command.append("--no-playlist")

    if job.audio_only:
        command.extend(["-x", "--audio-format", job.audio_format])
    elif job.format_id.strip():
        command.extend(["-f", job.format_id.strip()])
    else:
        command.extend(["-f", DEFAULT_VIDEO_FORMAT])

    extra_tokens = split_extra_args(job.extra_args)
    proxy, _, _ = current_proxy(job)
    if proxy:
        extra_tokens = strip_proxy_flags(extra_tokens)
    if extra_tokens:
        command.extend(extra_tokens)
    if proxy:
        command.extend(["--proxy", proxy])

    command.append(job.url)
    return command


def extract_output_file(line: str) -> str | None:
    clean = strip_ansi(line)
    if "Destination:" in clean:
        return clean.split("Destination:", 1)[1].strip()

    merge_match = re.search(r'Merging formats into "(.+?)"', clean)
    if merge_match:
        return merge_match.group(1)

    move_match = re.search(r'Moving file "(.+?)"', clean)
    if move_match:
        return move_match.group(1)

    return None


def is_cancel_requested(job_id: str) -> bool:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        return bool(job and job.cancel_requested)


def run_download(job_id: str) -> None:
    job_data = snapshot_job(job_id)
    if not job_data:
        return

    if job_data.get("cancel_requested"):
        update_job(job_id, status="canceled", message="任务已取消")
        return

    job = DownloadJob(**job_data)
    proxy, proxy_index, proxy_total = current_proxy(job)
    proxy_text = f" | 代理 {proxy_index + 1}/{proxy_total}" if proxy else ""

    try:
        command = build_command(job)
    except Exception as exc:
        update_job(job_id, status="failed", message=str(exc), progress=0.0)
        return

    attempt = job.retry_count + 1
    total_attempts = job.retry_max + 1

    update_job(
        job_id,
        status="running",
        message=f"开始下载 (尝试 {attempt}/{total_attempts}){proxy_text}",
        progress=0.0,
        active_proxy=proxy,
    )
    if proxy:
        append_log(job_id, f"[proxy] use {proxy_index + 1}/{proxy_total}: {proxy}")
    append_log(job_id, "$ " + " ".join(shlex.quote(part) for part in command))

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        errors="replace",
    )
    register_process(job_id, process)

    try:
        assert process.stdout is not None
        for line in process.stdout:
            append_log(job_id, line)

            progress_match = PROGRESS_RE.search(strip_ansi(line))
            if progress_match:
                progress = min(float(progress_match.group(1)), 100.0)
                update_job(job_id, progress=progress, message=f"下载中 {progress:.1f}%")

            output_file = extract_output_file(line)
            if output_file:
                update_job(job_id, output_file=output_file)

            if is_cancel_requested(job_id) and process.poll() is None:
                process.terminate()

        code = process.wait()

        if is_cancel_requested(job_id):
            update_job(job_id, status="canceled", message="任务已取消")
        elif code == 0:
            update_job(job_id, status="done", progress=100.0, message="下载完成")
            if job.auto_open_output:
                with JOBS_LOCK:
                    latest = JOBS.get(job_id)
                    output_dir_text = latest.output_dir if latest else job.output_dir
                    output_file = latest.output_file if latest else job.output_file
                try:
                    opened_path = open_output_path(output_dir_text, output_file)
                    append_log(job_id, f"[open-output] 自动打开目录: {opened_path}")
                    update_job(job_id, message="下载完成，已自动打开目录")
                except Exception as exc:
                    append_log(job_id, f"[open-output] 自动打开目录失败: {exc}")
                    update_job(job_id, message="下载完成（自动打开目录失败）")
        elif schedule_retry(job_id, f"下载失败，退出码 {code}"):
            pass
        else:
            update_job(job_id, status="failed", message=f"下载失败，退出码 {code}")
    finally:
        remove_process(job_id)


def parse_formats(info: dict[str, Any]) -> list[dict[str, Any]]:
    raw_formats = info.get("formats") or []
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for fmt in raw_formats:
        format_id = str(fmt.get("format_id") or "").strip()
        if not format_id or format_id in seen:
            continue

        seen.add(format_id)

        ext = str(fmt.get("ext") or "")
        resolution = str(fmt.get("resolution") or "")
        width = fmt.get("width")
        height = fmt.get("height")
        if not resolution and isinstance(width, int) and isinstance(height, int):
            resolution = f"{width}x{height}"

        vcodec = str(fmt.get("vcodec") or "")
        acodec = str(fmt.get("acodec") or "")
        is_audio = vcodec == "none" and acodec not in {"", "none"}

        if not resolution:
            if is_audio:
                resolution = "audio"
            elif vcodec == "none":
                resolution = "unknown"

        fps = fmt.get("fps")
        tbr = fmt.get("tbr")
        note = str(fmt.get("format_note") or "")
        size = fmt.get("filesize") or fmt.get("filesize_approx")

        label_parts = [format_id]
        if ext:
            label_parts.append(ext)
        if resolution:
            label_parts.append(resolution)
        if isinstance(fps, (int, float)) and fps > 0:
            label_parts.append(f"{int(fps)}fps")
        if isinstance(tbr, (int, float)) and tbr > 0:
            label_parts.append(f"{int(tbr)}kbps")
        if note:
            label_parts.append(note)
        size_text = fmt_size(size)
        if size_text:
            label_parts.append(size_text)

        score = 0
        if isinstance(height, int):
            score += height * 10
        if isinstance(tbr, (int, float)):
            score += int(tbr)

        items.append(
            {
                "id": format_id,
                "label": " | ".join(label_parts),
                "is_audio": is_audio,
                "score": score,
            }
        )

    items.sort(key=lambda item: item["score"], reverse=True)

    for item in items:
        item.pop("score", None)

    presets = [
        {"id": DEFAULT_VIDEO_FORMAT, "label": "推荐: 最佳画质自动合并", "is_audio": False},
        {"id": "best", "label": "推荐: 最高兼容单文件", "is_audio": False},
    ]
    return presets + items[:220]


def parse_info(url: str, playlist: bool, extra_args: str, proxy: str | None = None) -> dict[str, Any]:
    yt_dlp_cmd = get_yt_dlp_cmd()
    if not yt_dlp_cmd:
        raise RuntimeError("未检测到 yt-dlp，请先安装（python3 -m pip install -U yt-dlp）")

    command = [
        *yt_dlp_cmd,
        "--dump-single-json",
        "--skip-download",
        "--no-warnings",
    ]
    if not playlist:
        command.append("--no-playlist")

    extra_tokens = split_extra_args(extra_args)
    if proxy:
        extra_tokens = strip_proxy_flags(extra_tokens)
    if extra_tokens:
        command.extend(extra_tokens)
    if proxy:
        command.extend(["--proxy", proxy])

    command.append(url)

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        timeout=90,
        errors="replace",
    )

    if completed.returncode != 0:
        err = completed.stderr.strip() or completed.stdout.strip() or "解析失败"
        raise RuntimeError(err.splitlines()[-1])

    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"返回数据无法解析：{exc}") from exc

    entries = payload.get("entries") if isinstance(payload, dict) else None
    entry_count = len(entries) if isinstance(entries, list) else 0

    return {
        "title": payload.get("title") or "-",
        "uploader": payload.get("uploader") or payload.get("channel") or "-",
        "duration": fmt_duration(payload.get("duration")),
        "webpage_url": payload.get("webpage_url") or url,
        "is_playlist": payload.get("_type") == "playlist",
        "entries": entry_count,
        "formats": parse_formats(payload if isinstance(payload, dict) else {}),
    }


def collect_urls(payload: dict[str, Any]) -> list[str]:
    urls: list[str] = []

    single_url = str(payload.get("url", "")).strip()
    if single_url:
        urls.append(single_url)

    raw_urls = payload.get("urls")
    if isinstance(raw_urls, str):
        urls.extend([line.strip() for line in raw_urls.splitlines() if line.strip()])
    elif isinstance(raw_urls, list):
        urls.extend([str(item).strip() for item in raw_urls if str(item).strip()])

    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        deduped.append(url)

    return deduped


@app.get("/")
def index() -> str:
    return render_template(
        "index.html",
        default_output_dir=DEFAULT_OUTPUT_DIR,
        default_video_format=DEFAULT_VIDEO_FORMAT,
        default_retry_max=DEFAULT_RETRY_MAX,
        default_retry_strategy=DEFAULT_RETRY_STRATEGY,
        default_retry_delay_seconds=DEFAULT_RETRY_DELAY_SECONDS,
        default_retry_max_delay_seconds=DEFAULT_RETRY_MAX_DELAY_SECONDS,
        default_auto_open_output=DEFAULT_AUTO_OPEN_OUTPUT,
    )


@app.get("/api/system")
def system_status() -> Any:
    yt_cmd = get_yt_dlp_cmd()
    available = yt_cmd is not None
    return jsonify(
        {
            "yt_dlp_available": available,
            "hint": None if available else "未找到 yt-dlp，请先安装：python3 -m pip install -U yt-dlp",
            "command": " ".join(yt_cmd) if yt_cmd else None,
        }
    )


@app.post("/api/info")
def get_info() -> Any:
    payload = request.get_json(silent=True) or {}
    url = str(payload.get("url", "")).strip()
    if not url:
        return jsonify({"error": "请先输入视频链接"}), 400

    playlist = bool(payload.get("playlist", False))
    extra_args = str(payload.get("extra_args", "")).strip()
    proxy_pool = normalize_proxy_pool(payload.get("proxy_pool", []))
    proxy = proxy_pool[0] if proxy_pool else None

    try:
        info = parse_info(url=url, playlist=playlist, extra_args=extra_args, proxy=proxy)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(info)


@app.post("/api/download")
def create_download() -> Any:
    ensure_worker_started()
    payload = request.get_json(silent=True) or {}
    urls = collect_urls(payload)
    if not urls:
        return jsonify({"error": "请至少输入一个视频链接"}), 400

    output_dir = str(payload.get("output_dir", DEFAULT_OUTPUT_DIR)).strip() or DEFAULT_OUTPUT_DIR
    audio_only = bool(payload.get("audio_only", False))
    audio_format = str(payload.get("audio_format", "mp3")).strip() or "mp3"
    format_id = str(payload.get("format_id", "")).strip()
    extra_args = str(payload.get("extra_args", "")).strip()
    playlist = bool(payload.get("playlist", False))
    retry_max = clamp_retry_max(payload.get("retry_max", DEFAULT_RETRY_MAX))
    retry_strategy = normalize_retry_strategy(payload.get("retry_strategy", DEFAULT_RETRY_STRATEGY))
    retry_delay_seconds = clamp_retry_delay_seconds(payload.get("retry_delay_seconds", DEFAULT_RETRY_DELAY_SECONDS))
    retry_max_delay_seconds = clamp_retry_max_delay_seconds(
        payload.get("retry_max_delay_seconds", DEFAULT_RETRY_MAX_DELAY_SECONDS)
    )
    auto_open_output = bool(payload.get("auto_open_output", DEFAULT_AUTO_OPEN_OUTPUT))
    proxy_pool = normalize_proxy_pool(payload.get("proxy_pool", []))
    if retry_strategy == "fixed":
        retry_max_delay_seconds = max(retry_max_delay_seconds, retry_delay_seconds)

    created_ids: list[str] = []
    with JOBS_LOCK:
        for url in urls:
            job_id = uuid.uuid4().hex[:10]
            job = DownloadJob(
                id=job_id,
                url=url,
                output_dir=output_dir,
                audio_only=audio_only,
                audio_format=audio_format,
                format_id=format_id,
                extra_args=extra_args,
                playlist=playlist,
                auto_open_output=auto_open_output,
                retry_max=retry_max,
                retry_strategy=retry_strategy,
                retry_delay_seconds=retry_delay_seconds,
                retry_max_delay_seconds=retry_max_delay_seconds,
                proxy_pool=proxy_pool,
            )
            JOBS[job_id] = job
            created_ids.append(job_id)
        prune_jobs_locked()

    for job_id in created_ids:
        JOB_QUEUE.put(job_id)

    response = {
        "count": len(created_ids),
        "job_ids": created_ids,
        "first_job_id": created_ids[0],
        "queue_mode": "sequential",
    }
    if len(created_ids) == 1:
        response["job_id"] = created_ids[0]
    return jsonify(response)


@app.get("/api/jobs")
def list_jobs() -> Any:
    return jsonify({"jobs": list_jobs_snapshot()})


@app.get("/api/jobs/<job_id>")
def get_job(job_id: str) -> Any:
    job = snapshot_job(job_id)
    if not job:
        return jsonify({"error": "任务不存在"}), 404
    return jsonify(job)


@app.post("/api/jobs/<job_id>/cancel")
def cancel_job(job_id: str) -> Any:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404

        if job.status in {"done", "failed", "canceled"}:
            return jsonify({"error": f"当前状态不可取消：{job.status}"}), 400

        job.cancel_requested = True
        if job.status in {"queued", "retrying"}:
            job.status = "canceled"
            job.message = "任务已取消（队列中）"
            job.progress = 0.0
        else:
            job.status = "canceling"
            job.message = "正在取消..."
        job.updated_at = now_iso()
        process = PROCESSES.get(job_id)

    if process and process.poll() is None:
        try:
            process.terminate()
        except Exception:
            pass

    snapshot = snapshot_job(job_id)
    return jsonify(snapshot or {"ok": True})


@app.post("/api/jobs/<job_id>/requeue")
def requeue_job(job_id: str) -> Any:
    ensure_worker_started()
    with JOBS_LOCK:
        source = JOBS.get(job_id)
        if not source:
            return jsonify({"error": "任务不存在"}), 404

        if source.status in {"queued", "running", "canceling", "retrying"}:
            return jsonify({"error": f"当前状态不可重排队：{source.status}"}), 400

        new_id = uuid.uuid4().hex[:10]
        new_job = DownloadJob(
            id=new_id,
            url=source.url,
            output_dir=source.output_dir,
            audio_only=source.audio_only,
            audio_format=source.audio_format,
            format_id=source.format_id,
            extra_args=source.extra_args,
            playlist=source.playlist,
            auto_open_output=source.auto_open_output,
            retry_max=source.retry_max,
            retry_strategy=source.retry_strategy,
            retry_delay_seconds=source.retry_delay_seconds,
            retry_max_delay_seconds=source.retry_max_delay_seconds,
            proxy_pool=list(source.proxy_pool),
        )
        JOBS[new_id] = new_job
        prune_jobs_locked()

    JOB_QUEUE.put(new_id)
    return jsonify({"job_id": new_id, "from_job_id": job_id, "queue_mode": "sequential"})


@app.post("/api/jobs/<job_id>/open-output")
def open_output(job_id: str) -> Any:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify({"error": "任务不存在"}), 404
        output_dir_text = job.output_dir
        output_file = job.output_file

    try:
        target = open_output_path(output_dir_text, output_file)
    except Exception as exc:
        return jsonify({"error": f"打开目录失败：{exc}"}), 500

    return jsonify({"ok": True, "path": target})


if __name__ == "__main__":
    ensure_worker_started()
    app.run(host="127.0.0.1", port=17800, debug=True)
