import base64
import hashlib
import hmac
import secrets
import struct
import time


class CaptchaService:
    def __init__(self, key: bytes, ttl_seconds: int = 300):
        if len(key) < 32:
            raise ValueError("captcha key must be at least 32 bytes")
        self.key = key
        self.ttl_seconds = ttl_seconds

    def issue(self) -> tuple[str, str, str]:
        answer = "".join(secrets.choice("23456789ABCDEFGHJKLMNPQRSTUVWXYZ") for _ in range(4))
        expires = int(time.time()) + self.ttl_seconds
        nonce = secrets.token_bytes(16)
        payload = struct.pack(">Q", expires) + nonce + answer.encode("ascii")
        signature = hmac.new(self.key, payload, hashlib.sha256).digest()
        token = base64.urlsafe_b64encode(payload + signature).rstrip(b"=").decode("ascii")
        return token, answer, self._svg(answer)

    def verify(self, token: str, answer: str) -> bool:
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4))
            payload, signature = raw[:-32], raw[-32:]
            if not hmac.compare_digest(hmac.new(self.key, payload, hashlib.sha256).digest(), signature):
                return False
            expires = struct.unpack(">Q", payload[:8])[0]
            expected = payload[24:].decode("ascii")
        except (ValueError, UnicodeDecodeError, struct.error):
            return False
        return expires >= int(time.time()) and hmac.compare_digest(
            expected.casefold(), str(answer or "").strip().casefold()
        )

    @staticmethod
    def _svg(answer: str) -> str:
        escaped = "".join(char for char in answer if char.isalnum())
        return (
            '<svg xmlns="http://www.w3.org/2000/svg" width="180" height="58" viewBox="0 0 180 58" role="img" aria-label="验证码">'
            '<rect width="180" height="58" rx="12" fill="#f3efe5"/>'
            '<path d="M8 16L172 43M12 47L168 12M40 3L58 55M125 2L113 56" stroke="#b9b1a1" stroke-width="1" opacity=".55"/>'
            f'<text x="90" y="39" text-anchor="middle" font-family="monospace" font-size="28" font-weight="700" letter-spacing="8" fill="#354d48">{escaped}</text>'
            "</svg>"
        )
