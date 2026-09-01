from dataclasses import dataclass
from typing import Dict

from .secrets_store import SecretStore


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str

    def __repr__(self) -> str:
        return f"Credentials(username={self.username!r}, password=<redacted>)"


def resolve_tenant_credentials(config: Dict, owner_user_id: int, secret_store: SecretStore) -> Credentials:
    """Resolve credentials only when the opaque reference belongs to the owner."""
    if isinstance(owner_user_id, bool) or not isinstance(owner_user_id, int) or owner_user_id <= 0:
        raise ValueError("owner_user_id must be a positive integer")
    reference = str(config.get("credential_ref") or "")
    if not reference:
        raise LookupError("credential is not configured")
    expected_prefix = f"users/{owner_user_id}/credentials/"
    if not reference.startswith(expected_prefix):
        raise PermissionError("credential reference does not belong to tenant")
    username = str(config.get("username") or "").strip()
    password = secret_store.read(reference).rstrip("\r\n")
    if not username or not password:
        raise ValueError("未配置可用的登录凭据")
    return Credentials(username=username, password=password)
