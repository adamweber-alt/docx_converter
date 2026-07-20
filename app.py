import subprocess
import tempfile
import os
import re
import shutil
import signal
import threading
import time
import uuid
import logging
from flask import Flask, request, send_file, jsonify

app = Flask(__name__)

logging.basicConfig(
    filename="docxconversion.log",
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("convert")

MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_CONVERSIONS", 4))
CONVERT_TIMEOUT = int(os.environ.get("CONVERT_TIMEOUT_SECONDS", 90))
WATCHDOG_INTERVAL = 10  # seconds between sweeps
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE_BYTES", 25 * 1024 * 1024))  # 25MB

app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_SIZE

_semaphore = threading.Semaphore(MAX_CONCURRENT)

_active_jobs = {}
_active_jobs_lock = threading.Lock()


def _safe_filename(name: str, fallback: str) -> str:
    if not name:
        return fallback
    name = os.path.basename(name)
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name or fallback


#Keep and eye out for any hung soffice jobs
def _watchdog():
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        now = time.time()
        with _active_jobs_lock:
            stale = [
                (job_id, info) for job_id, info in _active_jobs.items()
                if now - info["started"] > CONVERT_TIMEOUT + 15
            ]
        for job_id, info in stale:
            pgid = info["pgid"]
            try:
                os.killpg(pgid, signal.SIGKILL)
                logger.warning("watchdog killed stale job job_id=%s pgid=%s", job_id, pgid)
            except ProcessLookupError:
                pass
            with _active_jobs_lock:
                _active_jobs.pop(job_id, None)


threading.Thread(target=_watchdog, daemon=True).start()

@app.errorhandler(413)
def too_large(e):
    logger.warning(
        "rejected oversized upload remote_addr=%s content_length=%s",
        request.remote_addr, request.content_length,
    )
    return jsonify({"error": f"file exceeds max size of {MAX_FILE_SIZE} bytes"}), 413

@app.route("/convert", methods=["POST"])
def convert():
    start_time = time.time()
    remote_addr = request.remote_addr

    if "file" not in request.files:
        logger.warning("convert rejected: no file provided remote_addr=%s", remote_addr)
        return jsonify({"error": "no file provided"}), 400

    f = request.files["file"]
    output_name = _safe_filename(
        request.form.get("output_name") or request.args.get("output_name"),
        fallback="converted.pdf",
    )
    orig_name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(f.filename or "unknown"))

    job_id = str(uuid.uuid4())
    tmpdir = tempfile.mkdtemp(prefix=f"conv_{job_id}_")
    profile_dir = os.path.join(tmpdir, "profile")

    logger.info(
        "convert start job_id=%s remote_addr=%s input=%s output=%s",
        job_id, remote_addr, orig_name, output_name,
    )

    acquired = _semaphore.acquire(timeout=CONVERT_TIMEOUT)
    if not acquired:
        shutil.rmtree(tmpdir, ignore_errors=True)
        logger.warning("convert busy job_id=%s remote_addr=%s", job_id, remote_addr)
        return jsonify({"error": "server busy, try again"}), 503

    try:
        in_path = os.path.join(tmpdir, "input.docx")
        f.save(in_path)

        #Caller might lie in the http content size
        actual_size = os.path.getsize(in_path)
        if actual_size > MAX_FILE_SIZE:
            logger.warning(
                "convert rejected oversized job_id=%s size=%s", job_id, actual_size
            )
            return jsonify({"error": "file exceeds max size"}), 413

        cmd = [
            "soffice", "--headless", "--norestore",
            f"-env:UserInstallation=file://{profile_dir}",
            "--convert-to", "pdf",
            "--outdir", tmpdir, in_path,
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        pgid = os.getpgid(proc.pid)

        with _active_jobs_lock:
            _active_jobs[job_id] = {"pgid": pgid, "started": time.time()}

        try:
            _, stderr = proc.communicate(timeout=CONVERT_TIMEOUT)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            logger.error("convert timeout job_id=%s", job_id)
            return jsonify({"error": "conversion timed out"}), 504
        finally:
            with _active_jobs_lock:
                _active_jobs.pop(job_id, None)

        out_path = os.path.join(tmpdir, "input.pdf")
        if proc.returncode != 0 or not os.path.exists(out_path):
            logger.error(
                "convert failed job_id=%s returncode=%s stderr=%s",
                job_id, proc.returncode, stderr.decode(errors="replace")[-500:],
            )
            return jsonify({
                "error": "conversion failed",
                "stderr": stderr.decode(errors="replace")[-2000:],
            }), 500

        elapsed = time.time() - start_time
        logger.info(
            "convert success job_id=%s elapsed=%.2fs output_size=%s",
            job_id, elapsed, os.path.getsize(out_path),
        )

        return send_file(
            out_path,
            mimetype="application/pdf",
            as_attachment=True,
            download_name=output_name,
        )
    finally:
        _semaphore.release()

        def _cleanup():
            time.sleep(5)
            shutil.rmtree(tmpdir, ignore_errors=True)

        threading.Thread(target=_cleanup, daemon=True).start()


@app.route("/health")
def health():
    with _active_jobs_lock:
        in_flight = len(_active_jobs)
    return jsonify({"status": "ok", "in_flight": in_flight, "max_concurrent": MAX_CONCURRENT})
