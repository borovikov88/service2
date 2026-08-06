import hashlib
import zipfile
from pathlib import Path, PurePath, PurePosixPath

from django.core.exceptions import ValidationError

MAX_XLSX_SIZE = 15 * 1024 * 1024
MAX_UNCOMPRESSED_SIZE = 150 * 1024 * 1024
MAX_ZIP_ENTRIES = 2_000
MAX_COMPRESSION_RATIO = 200
REQUIRED_XLSX_PARTS = {
    "[Content_Types].xml",
    "_rels/.rels",
    "xl/workbook.xml",
    "xl/_rels/workbook.xml.rels",
}


def safe_original_filename(name):
    cleaned = PurePath(str(name or "report.xlsx").replace("\\", "/")).name
    cleaned = "".join(char for char in cleaned if char.isprintable() and char not in '<>:"/\\|?*')
    return (cleaned or "report.xlsx")[:255]


def stream_sha256(uploaded_file):
    digest = hashlib.sha256()
    uploaded_file.seek(0)
    chunks = uploaded_file.chunks() if hasattr(uploaded_file, "chunks") else iter(lambda: uploaded_file.read(1024 * 1024), b"")
    for chunk in chunks:
        digest.update(chunk)
    uploaded_file.seek(0)
    return digest.hexdigest()


def validate_xlsx_archive(file_obj, *, size=None, filename=None):
    filename = safe_original_filename(filename or getattr(file_obj, "name", ""))
    if Path(filename).suffix.lower() != ".xlsx":
        raise ValidationError("Разрешены только файлы XLSX без макросов.")
    if size is not None and size > MAX_XLSX_SIZE:
        raise ValidationError("Размер файла превышает 15 МБ.")
    file_obj.seek(0)
    if file_obj.read(4) != b"PK\x03\x04":
        file_obj.seek(0)
        raise ValidationError("Файл не является корректным XLSX-архивом.")
    file_obj.seek(0)
    try:
        with zipfile.ZipFile(file_obj) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ZIP_ENTRIES:
                raise ValidationError("В XLSX слишком много внутренних файлов.")
            names = {entry.filename for entry in entries}
            if not REQUIRED_XLSX_PARTS.issubset(names):
                raise ValidationError("Файл не содержит обязательную структуру XLSX.")
            total = 0
            for entry in entries:
                path = PurePosixPath(entry.filename.replace("\\", "/"))
                if path.is_absolute() or ".." in path.parts:
                    raise ValidationError("XLSX содержит небезопасный внутренний путь.")
                if entry.flag_bits & 0x1:
                    raise ValidationError("Зашифрованные XLSX не поддерживаются.")
                total += entry.file_size
                if total > MAX_UNCOMPRESSED_SIZE:
                    raise ValidationError("Распакованный XLSX превышает безопасный лимит.")
                if entry.compress_size and entry.file_size / entry.compress_size > MAX_COMPRESSION_RATIO:
                    raise ValidationError("XLSX содержит подозрительно сжатые данные.")
                if entry.filename.lower().endswith(("vbaproject.bin", ".vba")):
                    raise ValidationError("XLSX с макросами не поддерживается.")
            if archive.testzip() is not None:
                raise ValidationError("XLSX содержит повреждённый внутренний файл.")
    except ValidationError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, RuntimeError, OSError, NotImplementedError) as exc:
        raise ValidationError("Повреждённый или неподдерживаемый XLSX-файл.") from exc
    finally:
        file_obj.seek(0)


def delete_private_file(storage, name, *, organization_id, batch_id):
    """Delete only a file located in the specified server-generated batch directory."""
    name = str(name or "").replace("\\", "/")
    if not name:
        return False
    path = PurePosixPath(name)
    expected = ("onec_imports", str(organization_id), str(batch_id))
    if path.is_absolute() or ".." in path.parts or tuple(path.parts[:3]) != expected or len(path.parts) != 4:
        return False
    storage.delete(name)
    return True


def delete_private_batch_file(batch):
    """Delete only a file located in this batch's server-generated directory."""
    field_file = getattr(batch, "stored_file", None)
    return delete_private_file(
        field_file.storage,
        getattr(field_file, "name", ""),
        organization_id=batch.organization_id,
        batch_id=batch.id,
    )
