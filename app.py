from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
from email.parser import BytesParser
from email.policy import default
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BASE_DIR = Path(__file__).resolve().parent
BOT_DIR = BASE_DIR.parent / "ElcutCheckBot"

import sys

if str(BOT_DIR) not in sys.path:
    sys.path.insert(0, str(BOT_DIR))

import db  # type: ignore
import logic  # type: ignore


MATERIALS_DIR = str(BOT_DIR / "Материалы")

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
    }


def pseudo_user_id(fio: str) -> int:
    digest = hashlib.sha256(fio.encode("utf-8")).hexdigest()[:12]
    return int(digest, 16)


def subtask_variant(profile: dict[str, Any], subtask: str) -> str:
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
        if parsed.path in ("/", "/index.html"):
            self.send_file(BASE_DIR / "index.html")
            return
        if parsed.path == "/Front-VS-Behind-meter.png":
            self.send_file(BASE_DIR.parent / "Front-VS-Behind-meter.png")
            return
        if parsed.path == "/api/ping":
            self.send_json({"ok": True})
            return
        self.send_json({"ok": False, "error": "Страница не найдена."}, status=HTTPStatus.NOT_FOUND)

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
            self.send_json({"ok": False, "error": "Маршрут не найден."}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self.send_json({"ok": False, "error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_json({"ok": False, "error": f"Внутренняя ошибка: {exc}"}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def require_profile(self, fio: str) -> dict[str, Any]:
        profile = get_student_profile(fio)
        if not profile:
            raise ValueError("Пользователь с таким ФИО не найден.")
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


def run() -> None:
    os.chdir(BASE_DIR)
    db.init_db(DB_PATH)
    server = ThreadingHTTPServer(("127.0.0.1", 8000), AppHandler)
    print(f"Using database: {DB_PATH}")
    print("Open http://127.0.0.1:8000")
    server.serve_forever()


if __name__ == "__main__":
    run()
