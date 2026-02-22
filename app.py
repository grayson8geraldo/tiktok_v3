#!/usr/bin/env python3
"""
Веб-интерфейс для Video Pipeline: HEVC + Anchor.

Позволяет:
  - Загружать якорные ролики в пул
  - Загружать основное видео для обработки
  - Запускать конвейер и скачивать результат
"""

import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import (
    Flask,
    flash,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)

from pipeline import batch_pipeline, run_pipeline

app = Flask(__name__)
app.secret_key = os.urandom(24)

# ─── Пути ────────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).resolve().parent
ANCHOR_POOL = BASE_DIR / "anchor_pool"
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "output"

ANCHOR_POOL.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

ALLOWED_EXT = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}

# ─── Состояние задач ─────────────────────────────────────────────────────────

jobs: dict[str, dict] = {}


def _allowed(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXT


# ─── Маршруты ────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    anchors = sorted(
        [f.name for f in ANCHOR_POOL.iterdir() if f.suffix.lower() in ALLOWED_EXT]
    )
    outputs = sorted(
        [f.name for f in OUTPUT_DIR.iterdir() if f.suffix.lower() in ALLOWED_EXT],
        reverse=True,
    )
    return render_template(
        "index.html",
        anchors=anchors,
        outputs=outputs,
        jobs=jobs,
    )


# ── Якоря ─────────────────────────────────────────────────────────────────────

@app.route("/upload-anchor", methods=["POST"])
def upload_anchor():
    files = request.files.getlist("anchors")
    if not files or files[0].filename == "":
        flash("Выберите хотя бы один файл", "warning")
        return redirect(url_for("index"))

    count = 0
    for f in files:
        if f and _allowed(f.filename):
            safe_name = Path(f.filename).name
            f.save(str(ANCHOR_POOL / safe_name))
            count += 1

    flash(f"Загружено якорей: {count}", "success")
    return redirect(url_for("index"))


@app.route("/delete-anchor/<name>", methods=["POST"])
def delete_anchor(name: str):
    path = ANCHOR_POOL / name
    if path.exists() and path.parent == ANCHOR_POOL:
        path.unlink()
        flash(f"Якорь «{name}» удалён", "success")
    return redirect(url_for("index"))


# ── Обработка видео ──────────────────────────────────────────────────────────

@app.route("/process", methods=["POST"])
def process_video():
    file = request.files.get("video")
    if not file or file.filename == "":
        flash("Выберите видео для обработки", "warning")
        return redirect(url_for("index"))

    if not _allowed(file.filename):
        flash("Неподдерживаемый формат файла", "danger")
        return redirect(url_for("index"))

    # Проверяем наличие якорей
    anchors = [f for f in ANCHOR_POOL.iterdir() if f.suffix.lower() in ALLOWED_EXT]
    if not anchors:
        flash("Сначала загрузите хотя бы один якорь!", "danger")
        return redirect(url_for("index"))

    count = int(request.form.get("count", 1))
    count = max(1, min(count, 50))

    # Сохраняем загруженное видео
    safe_name = Path(file.filename).name
    input_path = UPLOAD_DIR / safe_name
    file.save(str(input_path))

    # Создаём задачу
    job_id = uuid.uuid4().hex[:8]
    jobs[job_id] = {
        "id": job_id,
        "input": safe_name,
        "count": count,
        "status": "processing",
        "started": datetime.now().strftime("%H:%M:%S"),
        "results": [],
        "error": None,
    }

    # Запускаем обработку в фоне
    thread = threading.Thread(
        target=_run_job,
        args=(job_id, input_path, count),
        daemon=True,
    )
    thread.start()

    flash(f"Задача #{job_id} запущена: {count} версий из «{safe_name}»", "info")
    return redirect(url_for("index"))


def _run_job(job_id: str, input_path: Path, count: int):
    try:
        if count == 1:
            out_name = f"{input_path.stem}_{job_id}.mp4"
            out_path = OUTPUT_DIR / out_name
            run_pipeline(ANCHOR_POOL, input_path, out_path)
            jobs[job_id]["results"] = [out_name]
        else:
            sub_dir = OUTPUT_DIR / job_id
            results = batch_pipeline(ANCHOR_POOL, input_path, sub_dir, count)
            jobs[job_id]["results"] = [r.name for r in results]
        jobs[job_id]["status"] = "done"
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)


# ── Скачивание ────────────────────────────────────────────────────────────────

@app.route("/download/<path:filename>")
def download(filename: str):
    # Поддерживаем как файлы в OUTPUT_DIR, так и во вложенных папках
    file_path = OUTPUT_DIR / filename
    if file_path.exists() and OUTPUT_DIR in file_path.resolve().parents or file_path.resolve().parent == OUTPUT_DIR:
        return send_from_directory(str(file_path.parent), file_path.name, as_attachment=True)
    flash("Файл не найден", "danger")
    return redirect(url_for("index"))


@app.route("/delete-output/<path:filename>", methods=["POST"])
def delete_output(filename: str):
    file_path = OUTPUT_DIR / filename
    if file_path.exists() and (OUTPUT_DIR in file_path.resolve().parents or file_path.resolve().parent == OUTPUT_DIR):
        file_path.unlink()
        flash(f"Файл «{filename}» удалён", "success")
    return redirect(url_for("index"))


# ─── Запуск ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
