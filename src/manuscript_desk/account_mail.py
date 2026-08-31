import smtplib
import ssl
from email.message import EmailMessage

from .secrets_store import SecretStore


class AccountMailer:
    def __init__(self, repository, secret_store: SecretStore, public_base_url: str):
        self.repository = repository
        self.secret_store = secret_store
        self.public_base_url = public_base_url.rstrip("/")

    def configured(self) -> bool:
        with self.repository.connection() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM system_smtp_config WHERE singleton=1 AND credential_ref IS NOT NULL"
                ).fetchone()
                is not None
            )

    def send_code(self, email: str, display_name: str, code: str, purpose: str) -> None:
        titles = {
            "register": "注册验证码",
            "password_reset": "重置密码验证码",
            "email_change": "更换邮箱验证码",
            "admin_email_setup": "管理员邮箱验证码",
            "invite": "账号激活验证码",
        }
        title = titles.get(purpose, "邮箱验证码")
        body = (
            f"{display_name or '您好'}：\n\n"
            f"你的 Manuscript Desk {title}是：{code}\n\n"
            "验证码在 10 分钟内有效，最多可尝试 5 次。若非本人操作，请忽略此邮件。"
        )
        self._send(email, f"Manuscript Desk · {title}", body)

    def send_invitation(self, email: str, display_name: str, token: str) -> None:
        url = f"{self.public_base_url}/accept-invite#token={token}"
        body = (
            f"{display_name or '您好'}：\n\n"
            "管理员邀请你加入 Manuscript Desk。请在 48 小时内打开以下地址，验证邮箱并设置自己的密码：\n\n"
            f"{url}\n\n"
            "该链接只能使用一次。管理员不会看到你设置的密码。"
        )
        self._send(email, "Manuscript Desk · 账号邀请", body)

    def _send(self, recipient: str, subject: str, body: str) -> None:
        with self.repository.connection() as conn:
            row = conn.execute("SELECT * FROM system_smtp_config WHERE singleton=1").fetchone()
        if not row or not row["credential_ref"]:
            raise RuntimeError("system SMTP is not configured")
        password = self.secret_store.read(row["credential_ref"])
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = (
            f"{row['sender_name']} <{row['sender_address']}>" if row["sender_name"] else row["sender_address"]
        )
        message["To"] = recipient
        message.set_content(body)
        context = ssl.create_default_context()
        if row["security_mode"] == "ssl":
            with smtplib.SMTP_SSL(row["host"], row["port"], timeout=20, context=context) as smtp:
                if row["username"]:
                    smtp.login(row["username"], password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(row["host"], row["port"], timeout=20) as smtp:
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
                if row["username"]:
                    smtp.login(row["username"], password)
                smtp.send_message(message)
