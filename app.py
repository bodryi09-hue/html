from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sqlite3
import zipfile
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
BOT_DIR = BASE_DIR.parent / "ElcutCheckBot"

import sys

if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import db  # type: ignore
import logic  # type: ignore


MATERIALS_DIR = str(BOT_DIR / "Материалы")
THEORY_DIR = BASE_DIR.parent / "Теория"

SUBTASKS = ["1.1", "1.2", "1.3", "2", "3"]
ALLOWED_EXTS = [".pbm", ".mod", ".des"]
MAX_FILE_SIZE_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_BATCHES = 10
REL_TOL = {
    "1.1": (1, 1, 1, 1, 1),
    "1.2": (1, 1, 1, 1, 1),
    "1.3": (1, 2, 3, 1, 2, 3),
    "2": (1,) * 21,
    "3": (1,) * 21,
}
TASK23_LABELS = {
    "2": [
        "C11", "C21", "C22", "C31", "C32", "C33", "C41", "C42", "C43", "C44",
        "C51", "C52", "C53", "C54", "C55", "C61", "C62", "C63", "C64", "C65", "C66",
    ],
    "3": [
        "L11", "L21", "L22", "L31", "L32", "L33", "L41", "L42", "L43", "L44",
        "L51", "L52", "L53", "L54", "L55", "L61", "L62", "L63", "L64", "L65", "L66",
    ],
}

REQUIRED_TABLES = {"Студенты", "Поля", "Решение 1", "Решение 2", "Решение 3"}


def has_required_tables(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        names = {row[0] for row in cur.fetchall()}
        conn.close()
        return REQUIRED_TABLES.issubset(names)
    except Exception:
        return False


def resolve_db_path() -> str:
    candidates = [
        BOT_DIR / "database.db",
        BOT_DIR / "database.db.db",
        BOT_DIR / "database_before_update.db",
    ]
    for candidate in candidates:
        if has_required_tables(candidate):
            return str(candidate)
    return str(BOT_DIR / "database.db")


DB_PATH = resolve_db_path()


def sanitize_path_part(name: str) -> str:
    value = (name or "").strip().replace("\x00", "")
    value = re.sub(r'[<>:"/\\|?*]+', "_", value)
    value = value.strip().strip(".")
    return value or "user"


def normalize_fio_input(text: str) -> str | None:
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return None
    parts = [part for part in text.split(" ") if part]
    if len(parts) < 2:
        return None

    def cap_word(word: str) -> str:
        chunks = []
        for item in word.split("-"):
            if item:
                chunks.append(item[:1].upper() + item[1:].lower())
            else:
                chunks.append(item)
        return "-".join(chunks)

    return " ".join(cap_word(part) for part in parts)


def get_student_profile(fio: str) -> dict[str, Any] | None:
    normalized = normalize_fio_input(fio)
    if not normalized:
        return None
    prof = db.find_student_by_fio(DB_PATH, normalized)
    if not prof:
        return None
    return {
        "fio": str(prof.get("fio") or "").strip(),
        "variant1": str(prof.get("variant1") or "").strip(),
        "variant23": str(prof.get("variant23") or "").strip(),
        "is_teacher": False,
    }


def get_teacher_profile(fio: str) -> dict[str, Any] | None:
    raw = (fio or "").strip()
    normalized = normalize_fio_input(raw)
    if raw.lower() == "преподаватель" or normalized == "Преподаватель":
        return {
            "fio": "Преподаватель",
            "variant1": "0",
            "variant23": "0",
            "is_teacher": True,
        }
    return None


def pseudo_user_id(fio: str) -> int:
    digest = hashlib.sha256(fio.encode("utf-8")).hexdigest()[:12]
    return int(digest, 16)


def subtask_variant(profile: dict[str, Any], subtask: str) -> str:
    if profile.get("is_teacher"):
        return "0"
    if subtask in ("1.1", "1.2", "1.3"):
        return str(profile.get("variant1") or "").strip()
    return str(profile.get("variant23") or "").strip()


def format_status(profile: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for subtask in SUBTASKS:
        variant = subtask_variant(profile, subtask)
        summary = db.get_attempt_summary(DB_PATH, variant, subtask) if variant else {
            "count": 0,
            "first_ok": "",
            "last_time": "",
            "last_success": 0,
        }
        upload_count = db.count_upload_batches(DB_PATH, variant, subtask) if variant else 0
        rows.append({
            "subtask": subtask,
            "variant": variant,
            "attempt_count": int(summary.get("count") or 0),
            "accepted_at": str(summary.get("first_ok") or ""),
            "last_attempt_at": str(summary.get("last_time") or ""),
            "last_attempt_success": bool(summary.get("last_success")),
            "upload_count": int(upload_count),
        })
    return rows


def build_task_payload(profile: dict[str, Any], subtask: str) -> dict[str, Any]:
    variant = subtask_variant(profile, subtask)
    if subtask in ("1.1", "1.2", "1.3"):
        fields = db.get_fields(DB_PATH, subtask)
    else:
        fields = TASK23_LABELS[subtask]
    return {
        "subtask": subtask,
        "variant": variant,
        "fields": fields,
        "tolerance_pct": list(REL_TOL[subtask]),
    }


def get_any_profile(fio: str) -> dict[str, Any] | None:
    return get_teacher_profile(fio) or get_student_profile(fio)


def get_students_report_rows() -> list[dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cols = db.get_students_columns(DB_PATH)
    mapping = db._students_col_map(cols)  # type: ignore[attr-defined]
    fio_col = mapping.get("fio") or ""
    v1_col = mapping.get("variant1") or ""
    v23_col = mapping.get("variant23") or ""
    group_col = cols[2] if len(cols) >= 3 else ("№ гр." if "№ гр." in cols else ("Группа" if "Группа" in cols else ""))
    if not fio_col:
        conn.close()
        return []
    cur.execute('SELECT * FROM "Студенты" ORDER BY ' + db.qname(fio_col))
    rows: list[dict[str, Any]] = []
    for row in cur.fetchall():
        fio = str(row[fio_col] or "").strip()
        variant1 = str(row[v1_col] or "").strip() if v1_col else ""
        variant23 = str(row[v23_col] or "").strip() if v23_col else ""
        tasks: list[dict[str, Any]] = []
        for subtask in SUBTASKS:
            variant = variant1 if subtask in ("1.1", "1.2", "1.3") else variant23
            summary = db.get_attempt_summary(DB_PATH, variant, subtask) if variant else {"count": 0, "first_ok": "", "last_time": "", "last_success": 0}
            uploads = db.count_upload_batches(DB_PATH, variant, subtask) if variant else 0
            tasks.append({
                "subtask": subtask,
                "variant": variant,
                "accepted_at": str(summary.get("first_ok") or ""),
                "attempt_count": int(summary.get("count") or 0),
                "upload_count": int(uploads),
                "last_attempt_at": str(summary.get("last_time") or ""),
                "accepted": bool(summary.get("first_ok")),
            })
        has_materials = (Path(MATERIALS_DIR) / sanitize_path_part(fio)).exists()
        rows.append({
            "fio": fio,
            "group": str(row[group_col] or "").strip() if group_col else "",
            "variant1": variant1,
            "variant23": variant23,
            "tasks": tasks,
            "has_materials": has_materials,
            "download_url": f"/teacher/materials/{quote(fio)}",
        })
    conn.close()
    return rows


def build_student_materials_zip(student_fio: str) -> tuple[bytes, str]:
    folder = Path(MATERIALS_DIR) / sanitize_path_part(student_fio)
    if not folder.exists() or not folder.is_dir():
        raise ValueError("Материалы студента не найдены.")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for root, _, files in os.walk(folder):
            for filename in files:
                full = Path(root) / filename
                archive.write(full, arcname=str(full.relative_to(folder.parent)))
    archive_name = sanitize_path_part(student_fio) + ".zip"
    return buffer.getvalue(), archive_name


def replicate_variant_files(variant: str, subtask: str, src_dir: str, filenames: list[str], uploader_fio: str) -> None:
    if subtask not in ("2", "3") or not variant:
        return
    try:
        peers = db.get_students_by_variant23(DB_PATH, variant)
    except Exception:
        peers = []
    for peer in peers:
        fio = str(peer.get("fio") or "").strip()
        if not fio or fio == uploader_fio:
            continue
        dest_dir = os.path.join(MATERIALS_DIR, sanitize_path_part(fio), subtask)
        os.makedirs(dest_dir, exist_ok=True)
        for name in filenames:
            src_path = os.path.join(src_dir, name)
            dest_path = os.path.join(dest_dir, name)
            try:
                shutil.copy2(src_path, dest_path)
            except Exception:
                pass


def unique_path(dir_path: str, filename: str) -> str:
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(dir_path, filename)
    if not os.path.exists(candidate):
        return candidate
    index = 1
    while True:
        candidate = os.path.join(dir_path, f"{base} ({index}){ext}")
        if not os.path.exists(candidate):
            return candidate
        index += 1


def parse_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    payload = handler.rfile.read(length) if length else b"{}"
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8"))


def parse_multipart(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    content_type = handler.headers.get("Content-Type", "")
    length = int(handler.headers.get("Content-Length", "0"))
    if "boundary=" not in content_type:
        raise ValueError("Не удалось разобрать загружаемые файлы.")
    raw = handler.rfile.read(length)
    message = BytesParser(policy=default).parsebytes(
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8") + raw
    )
    fields: dict[str, Any] = {"files": []}
    for part in message.iter_parts():
        disposition = part.get_content_disposition()
        if disposition != "form-data":
            continue
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        data = part.get_payload(decode=True) or b""
        if filename:
            fields["files"].append({"name": filename, "content": data})
        elif name:
            fields[name] = data.decode("utf-8").strip()
    return fields


def theory_content_type(path: Path) -> str:
    if path.suffix == ".mkv":
        return "video/x-matroska"
    if path.suffix == ".xlsx":
        return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    if path.suffix == ".docx":
        return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if path.suffix == ".pdf":
        return "application/pdf"
    return "application/octet-stream"


def content_disposition(disposition: str, filename: str) -> str:
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]+', "_", filename).strip() or "download"
    return f"{disposition}; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


class AppHandler(BaseHTTPRequestHandler):
    server_version = "ElcutWeb/1.0"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        body = path.read_bytes()
        content_type = "text/plain; charset=utf-8"
        if path.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        elif path.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        elif path.suffix == ".js":
            content_type = "application/javascript; charset=utf-8"
        elif path.suffix == ".png":
            content_type = "image/png"
        elif path.suffix in (".jpg", ".jpeg"):
            content_type = "image/jpeg"
        elif path.suffix == ".svg":
            content_type = "image/svg+xml"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                self.send_file(BASE_DIR / "index.html")
                return
            if parsed.path == "/Front-VS-Behind-meter.png":
                self.send_file(BASE_DIR.parent / "Front-VS-Behind-meter.png")
                return
            if parsed.path in ("/favicon.png", "/pngwing.com.png"):
                self.send_file(BASE_DIR.parent / "pngwing.com.png")
                return
            if parsed.path == "/api/theory":
                self.handle_theory_list()
                return
            if parsed.path.startswith("/theory/"):
                self.handle_theory_file(parsed.path)
                return
            if parsed.path.startswith("/teacher/materials/"):
                self.handle_teacher_materials(parsed.path)
                return
            if parsed.path == "/api/ping":
                self.send_json({"ok": True})
                return
            self.send_json({"ok": False, "error": "Страница не найдена."}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"ok": False, "error": f"Внутренняя ошибка: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/login":
                self.handle_login()
                return
            if parsed.path == "/api/status":
                self.handle_status()
                return
            if parsed.path == "/api/check":
                self.handle_check()
                return
            if parsed.path == "/api/upload":
                self.handle_upload()
                return
            if parsed.path == "/api/teacher/summary":
                self.handle_teacher_summary()
                return
            self.send_json({"ok": False, "error": "Маршрут не найден."}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"ok": False, "error": f"Внутренняя ошибка: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def handle_theory_list(self) -> None:
        items: list[dict[str, Any]] = []
        if THEORY_DIR.exists():
            for path in sorted(THEORY_DIR.iterdir(), key=lambda item: item.name.lower()):
                if not path.is_file():
                    continue
                preview_url = None
                if path.suffix in (".xlsx", ".docx"):
                    pdf_candidate = path.with_suffix(".pdf")
                    if pdf_candidate.exists():
                        preview_url = f"/theory/{pdf_candidate.name}"
                elif path.suffix == ".pdf":
                    preview_url = f"/theory/{path.name}"
                items.append({
                    "name": path.name,
                    "size": path.stat().st_size,
                    "url": f"/theory/{path.name}",
                    "preview_url": preview_url,
                })
        self.send_json({"ok": True, "files": items})

    def handle_theory_file(self, raw_path: str) -> None:
        filename = unquote(raw_path.removeprefix("/theory/"))
        safe_name = os.path.basename(filename)
        path = THEORY_DIR / safe_name
        if not path.exists() or not path.is_file():
            self.send_json({"ok": False, "error": "Файл теории не найден."}, status=HTTPStatus.NOT_FOUND)
            return
        content_type = theory_content_type(path)
        total_size = path.stat().st_size
        range_header = self.headers.get("Range", "").strip()
        if range_header.startswith("bytes="):
            start_s, _, end_s = range_header[6:].partition("-")
            try:
                start = int(start_s) if start_s else 0
                end = int(end_s) if end_s else total_size - 1
            except ValueError:
                start = 0
                end = total_size - 1
            start = max(0, min(start, total_size - 1))
            end = max(start, min(end, total_size - 1))
            chunk_size = end - start + 1
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Type", content_type)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
            self.send_header("Content-Length", str(chunk_size))
            self.end_headers()
            with open(path, "rb") as stream:
                stream.seek(start)
                self.wfile.write(stream.read(chunk_size))
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Accept-Ranges", "bytes")
        disposition = "inline" if path.suffix in (".mkv", ".pdf") else "attachment"
        self.send_header("Content-Disposition", content_disposition(disposition, path.name))
        self.send_header("Content-Length", str(total_size))
        self.end_headers()
        with open(path, "rb") as stream:
            shutil.copyfileobj(stream, self.wfile)

    def require_profile(self, fio: str) -> dict[str, Any]:
        profile = get_any_profile(fio)
        if not profile:
            raise ValueError("Пользователь с таким ФИО не найден.")
        return profile

    def require_teacher(self, fio: str) -> dict[str, Any]:
        profile = get_teacher_profile(fio)
        if not profile:
            raise ValueError("Режим итогов доступен только преподавателю.")
        return profile

    def handle_login(self) -> None:
        data = parse_json_body(self)
        profile = self.require_profile(str(data.get("fio") or ""))
        payload = {
            "ok": True,
            "profile": profile,
            "status": format_status(profile),
            "tasks": {subtask: build_task_payload(profile, subtask) for subtask in SUBTASKS},
        }
        self.send_json(payload)

    def handle_status(self) -> None:
        data = parse_json_body(self)
        profile = self.require_profile(str(data.get("fio") or ""))
        self.send_json({"ok": True, "status": format_status(profile)})

    def handle_teacher_summary(self) -> None:
        data = parse_json_body(self)
        self.require_teacher(str(data.get("fio") or ""))
        self.send_json({"ok": True, "rows": get_students_report_rows()})

    def handle_check(self) -> None:
        data = parse_json_body(self)
        subtask = str(data.get("subtask") or "").strip()
        if subtask not in SUBTASKS:
            raise ValueError("Неизвестная задача.")

        profile = self.require_profile(str(data.get("fio") or ""))
        variant = subtask_variant(profile, subtask)
        fields = build_task_payload(profile, subtask)["fields"]
        values = data.get("values")
        if not isinstance(values, list):
            raise ValueError("Нужно передать массив чисел.")
        parsed_values = logic.parse_numbers(" ".join(str(x) for x in values), expected_count=len(fields))
        if parsed_values is None:
            raise ValueError("Неверный формат чисел.")

        if subtask in ("1.1", "1.2", "1.3"):
            ref = db.get_solution_task1(DB_PATH, variant, subtask)
        else:
            ref = db.get_solution_task23(DB_PATH, variant, subtask)
        if not ref:
            raise ValueError("Не удалось получить эталонные значения из базы.")

        errors = logic.rel_errors_pct(parsed_values, ref)
        if errors is None:
            raise ValueError("Не удалось посчитать погрешности.")
        rounded_errors = [round(float(item), 2) for item in errors]
        tolerance = list(REL_TOL[subtask])
        ok = logic.check_subtask_rel(parsed_values, ref, tolerance)
        db.add_attempt(
            DB_PATH,
            variant,
            subtask,
            pseudo_user_id(profile["fio"]),
            profile["fio"],
            ok,
            parsed_values,
            rounded_errors,
        )
        self.send_json({
            "ok": True,
            "accepted": ok,
            "errors_pct": rounded_errors,
            "status": format_status(profile),
        })

    def handle_upload(self) -> None:
        data = parse_multipart(self)
        fio = str(data.get("fio") or "")
        subtask = str(data.get("subtask") or "").strip()
        files = data.get("files") or []
        if subtask not in SUBTASKS:
            raise ValueError("Неизвестная задача для загрузки.")
        if len(files) != 3:
            raise ValueError("Нужно загрузить ровно 3 файла: .pbm, .mod и .des.")

        profile = self.require_profile(fio)
        variant = subtask_variant(profile, subtask)
        if db.count_upload_batches(DB_PATH, variant, subtask) >= MAX_UPLOAD_BATCHES:
            raise ValueError("Превышен лимит загрузок для этой задачи.")

        normalized_files: dict[str, dict[str, Any]] = {}
        stem: str | None = None
        for item in files:
            filename = os.path.basename(str(item.get("name") or "").strip())
            ext = Path(filename).suffix.lower()
            cur_stem = Path(filename).stem
            content = item.get("content") or b""
            if ext not in ALLOWED_EXTS:
                raise ValueError("Разрешены только файлы .pbm, .mod и .des.")
            if len(content) > MAX_FILE_SIZE_BYTES:
                raise ValueError(f"Файл {filename} слишком большой.")
            if stem is None:
                stem = cur_stem
            if cur_stem != stem:
                raise ValueError("Имена файлов должны совпадать до расширения.")
            normalized_files[ext] = {"filename": filename, "content": content}

        if set(normalized_files) != set(ALLOWED_EXTS):
            raise ValueError("Нужны все три файла: .pbm, .mod и .des.")

        upload_dir = os.path.join(MATERIALS_DIR, sanitize_path_part(profile["fio"]), subtask)
        os.makedirs(upload_dir, exist_ok=True)

        saved_names: list[str] = []
        logged_files: list[dict[str, Any]] = []
        batch_name = stem or "work"
        for ext in ALLOWED_EXTS:
            final_name = batch_name + ext
            final_path = unique_path(upload_dir, final_name)
            with open(final_path, "wb") as stream:
                stream.write(normalized_files[ext]["content"])
            saved_name = os.path.basename(final_path)
            saved_names.append(saved_name)
            logged_files.append({
                "filename": normalized_files[ext]["filename"],
                "saved_as": saved_name,
                "size": len(normalized_files[ext]["content"]),
            })

        db.add_upload_batch(
            DB_PATH,
            variant,
            subtask,
            pseudo_user_id(profile["fio"]),
            profile["fio"],
            batch_name,
            logged_files,
        )
        replicate_variant_files(variant, subtask, upload_dir, saved_names, profile["fio"])
        self.send_json({
            "ok": True,
            "saved_files": saved_names,
            "status": format_status(profile),
        })

    def handle_teacher_materials(self, raw_path: str) -> None:
        fio = unquote(raw_path.removeprefix("/teacher/materials/"))
        body, archive_name = build_student_materials_zip(fio)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/zip")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Disposition", content_disposition("attachment", archive_name))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def run() -> None:
    os.chdir(BASE_DIR)
    db.init_db(DB_PATH)
    server = ThreadingHTTPServer(("127.0.0.1", 8000), AppHandler)
    print(f"Using database: {DB_PATH}")
    print("Open http://127.0.0.1:8000")
    server.serve_forever()


if __name__ == "__main__":
    run()
