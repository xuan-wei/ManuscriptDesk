import os
import secrets
from pathlib import Path


class SecretStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)

    def _user_dir(self, user_uuid: str, category: str) -> Path:
        if not user_uuid or any(char not in "0123456789abcdef-" for char in user_uuid.lower()):
            raise ValueError("invalid internal user identifier")
        if category not in {"credentials", "smtp", "tokens"}:
            raise ValueError("invalid secret category")
        return self.root / "users" / user_uuid / category

    def _secure_directory_chain(self, directory: Path) -> None:
        root = self.root.resolve()
        target = directory.resolve()
        if target != root and root not in target.parents:
            raise ValueError("invalid secret directory")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
        current = root
        for part in target.relative_to(root).parts:
            current = current / part
            current.mkdir(mode=0o700, exist_ok=True)
            os.chmod(current, 0o700)

    def write(self, user_uuid: str, category: str, value: str) -> str:
        if not value:
            raise ValueError("secret must not be empty")
        directory = self._user_dir(user_uuid, category)
        self._secure_directory_chain(directory)
        secret_id = secrets.token_hex(16)
        target = directory / secret_id
        temporary = directory / f".{secret_id}.{secrets.token_hex(8)}.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            os.chmod(target, 0o600)
            dir_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return f"users/{user_uuid}/{category}/{secret_id}"

    def delete(self, reference: str) -> None:
        relative = Path(reference)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid secret reference")
        target = (self.root / relative).resolve()
        root = self.root.resolve()
        if root not in target.parents:
            raise ValueError("invalid secret reference")
        target.unlink(missing_ok=True)

    def read(self, reference: str) -> str:
        relative = Path(reference)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid secret reference")
        target = (self.root / relative).resolve()
        root = self.root.resolve()
        if root not in target.parents:
            raise ValueError("invalid secret reference")
        mode = target.stat().st_mode & 0o777
        if mode & 0o077:
            raise PermissionError("secret file permissions are too broad")
        return target.read_text(encoding="utf-8")
