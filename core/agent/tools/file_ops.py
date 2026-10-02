"""Tool ``file_ops``: leer, escribir, listar, copiar, mover y borrar ficheros."""

from __future__ import annotations

import asyncio
import shutil
import stat
import time
from pathlib import Path
from typing import Any

import aiofiles

from .base import RiskLevel, Tool, ToolResult

# Rutas del sistema cuya modificación requiere confirmación explícita.
PROTECTED_PREFIXES = ("/etc", "/boot", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/var/lib", "/dev", "/sys", "/proc")

MAX_READ_BYTES = 200_000


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}PB"


class FileOpsTool(Tool):
    name = "file_ops"
    description = (
        "Manage files and directories. Actions: read (read a text file), write (create/overwrite a file), "
        "append (append text), list (list a directory), stat (file metadata), mkdir (create directory), "
        "copy, move, delete (file or directory)."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["read", "write", "append", "list", "stat", "mkdir", "copy", "move", "delete"],
                "description": "Operation to perform.",
            },
            "path": {"type": "string", "description": "Target file or directory path."},
            "content": {"type": "string", "description": "Text content for write/append."},
            "destination": {"type": "string", "description": "Destination path for copy/move."},
            "recursive": {"type": "boolean", "description": "Recursive list/delete.", "default": False},
            "max_lines": {"type": "integer", "description": "Max lines to return on read.", "default": 200},
        },
        "required": ["action", "path"],
    }
    risk = RiskLevel.ELEVATED

    def needs_confirmation(self, args: dict[str, Any]) -> str | None:
        action = args.get("action")
        path = str(Path(str(args.get("path", ""))).expanduser())
        if action == "delete":
            return f"Se va a borrar '{path}'" + (" de forma recursiva" if args.get("recursive") else "")
        if action in {"write", "move", "append"} and path.startswith(PROTECTED_PREFIXES):
            return f"Modificación de una ruta protegida del sistema: {path}"
        if action == "write" and Path(path).exists():
            return f"Se va a sobrescribir el fichero existente '{path}'"
        return None

    async def execute(self, action: str, path: str, content: str | None = None, destination: str | None = None,
                      recursive: bool = False, max_lines: int = 200) -> ToolResult:
        p = Path(path).expanduser()
        handler = getattr(self, f"_do_{action}", None)
        if handler is None:
            return ToolResult(False, error=f"Acción no soportada: {action}")
        try:
            return await handler(p, content=content, destination=destination, recursive=recursive,
                                 max_lines=max_lines)
        except FileNotFoundError:
            return ToolResult(False, error=f"No existe: {p}")
        except PermissionError:
            return ToolResult(False, error=f"Permiso denegado: {p}")
        except IsADirectoryError:
            return ToolResult(False, error=f"Es un directorio: {p}")
        except NotADirectoryError:
            return ToolResult(False, error=f"No es un directorio: {p}")

    async def _do_read(self, p: Path, max_lines: int = 200, **_: Any) -> ToolResult:
        size = p.stat().st_size
        async with aiofiles.open(p, "rb") as fh:
            raw = await fh.read(MAX_READ_BYTES)
        if b"\x00" in raw[:4096]:
            return ToolResult(False, error=f"'{p}' parece un fichero binario ({_human(size)})")
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines()
        note = ""
        if len(lines) > max_lines:
            note = f"\n... [{len(lines) - max_lines} líneas más; total {_human(size)}]"
            lines = lines[:max_lines]
        elif size > MAX_READ_BYTES:
            note = f"\n... [fichero truncado a {_human(MAX_READ_BYTES)} de {_human(size)}]"
        return ToolResult(True, output="\n".join(lines) + note, data={"path": str(p), "size": size})

    async def _do_write(self, p: Path, content: str | None = None, **_: Any) -> ToolResult:
        p.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(p, "w", encoding="utf-8") as fh:
            await fh.write(content or "")
        return ToolResult(True, output=f"Escritos {len(content or '')} caracteres en {p}")

    async def _do_append(self, p: Path, content: str | None = None, **_: Any) -> ToolResult:
        p.parent.mkdir(parents=True, exist_ok=True)
        async with aiofiles.open(p, "a", encoding="utf-8") as fh:
            await fh.write(content or "")
        return ToolResult(True, output=f"Añadidos {len(content or '')} caracteres a {p}")

    async def _do_list(self, p: Path, recursive: bool = False, **_: Any) -> ToolResult:
        def _list() -> list[str]:
            if not p.is_dir():
                raise NotADirectoryError(str(p))
            iterator = p.rglob("*") if recursive else p.iterdir()
            rows: list[str] = []
            for i, entry in enumerate(sorted(iterator)):
                if i >= 500:
                    rows.append("... [listado truncado a 500 entradas]")
                    break
                try:
                    st = entry.lstat()
                    kind = "d" if stat.S_ISDIR(st.st_mode) else ("l" if stat.S_ISLNK(st.st_mode) else "-")
                    rows.append(f"{kind} {stat.filemode(st.st_mode)} {_human(st.st_size):>8}  "
                                f"{entry.relative_to(p) if recursive else entry.name}")
                except OSError:
                    rows.append(f"? {entry.name}")
            return rows

        rows = await asyncio.to_thread(_list)
        return ToolResult(True, output="\n".join(rows) or "(directorio vacío)", data={"count": len(rows)})

    async def _do_stat(self, p: Path, **_: Any) -> ToolResult:
        st = p.stat()
        info = {
            "path": str(p.resolve()),
            "type": "directory" if p.is_dir() else "file" if p.is_file() else "other",
            "size": st.st_size,
            "mode": stat.filemode(st.st_mode),
            "uid": st.st_uid,
            "gid": st.st_gid,
            "modified": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
        }
        return ToolResult(True, output="\n".join(f"{k}: {v}" for k, v in info.items()), data=info)

    async def _do_mkdir(self, p: Path, **_: Any) -> ToolResult:
        p.mkdir(parents=True, exist_ok=True)
        return ToolResult(True, output=f"Directorio creado: {p}")

    async def _do_copy(self, p: Path, destination: str | None = None, **_: Any) -> ToolResult:
        if not destination:
            return ToolResult(False, error="'destination' es obligatorio para copy")
        dest = Path(destination).expanduser()
        if p.is_dir():
            await asyncio.to_thread(shutil.copytree, p, dest, dirs_exist_ok=True)
        else:
            await asyncio.to_thread(shutil.copy2, p, dest)
        return ToolResult(True, output=f"Copiado {p} -> {dest}")

    async def _do_move(self, p: Path, destination: str | None = None, **_: Any) -> ToolResult:
        if not destination:
            return ToolResult(False, error="'destination' es obligatorio para move")
        dest = Path(destination).expanduser()
        await asyncio.to_thread(shutil.move, str(p), str(dest))
        return ToolResult(True, output=f"Movido {p} -> {dest}")

    async def _do_delete(self, p: Path, recursive: bool = False, **_: Any) -> ToolResult:
        if str(p.resolve()) in {"/", str(Path.home())}:
            return ToolResult(False, error=f"Rechazado: no se permite borrar {p}")
        if p.is_dir() and not p.is_symlink():
            if recursive:
                await asyncio.to_thread(shutil.rmtree, p)
            else:
                p.rmdir()  # solo si está vacío
        else:
            p.unlink()
        return ToolResult(True, output=f"Borrado: {p}")
