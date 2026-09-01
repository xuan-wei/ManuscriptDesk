import atexit
import html
import logging
import smtplib
import ssl
import threading
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .repository import Repository, manuscript_fingerprint
from .secrets_store import SecretStore
from .security import random_token, token_hash
from .tracking_service import TenantRunContext, TrackingService

logger = logging.getLogger(__name__)


class AutomationRuntime:
    """Runs tenant schedules and serially executes the persistent run queue."""

    def __init__(self, repository: Repository, secret_store: SecretStore, public_base_url: str):
        self.repository = repository
        self.secret_store = secret_store
        self.public_base_url = public_base_url.rstrip("/")
        self.tracking = TrackingService(repository, secret_store)
        self.scheduler = BackgroundScheduler(timezone="UTC")
        self.worker_id = f"automation-{random_token(12)}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._reload_jobs()
            self.scheduler.add_job(
                self._reload_jobs,
                "interval",
                seconds=60,
                id="reload-automation-jobs",
                replace_existing=True,
                max_instances=1,
            )
            self.scheduler.start()
            self._thread = threading.Thread(target=self._worker_loop, name="tenant-automation-worker", daemon=True)
            self._thread.start()
            atexit.register(self.stop)
            logger.info("tenant automation runtime started")

    def stop(self) -> None:
        self._stop.set()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=10)
        self._thread = None

    def _reload_jobs(self) -> None:
        expected = {"reload-automation-jobs"}
        with self.repository.connection() as conn:
            rows = conn.execute(
                "SELECT id,owner_user_id,kind,cron_expression,timezone FROM automation_jobs WHERE enabled=1"
            ).fetchall()
        for row in rows:
            scheduler_id = f"tenant-{row['owner_user_id']}-job-{row['id']}"
            expected.add(scheduler_id)
            try:
                trigger = CronTrigger.from_crontab(row["cron_expression"], timezone=row["timezone"])
                self.scheduler.add_job(
                    self._enqueue_scheduled,
                    trigger,
                    id=scheduler_id,
                    args=[row["owner_user_id"], row["id"], row["kind"]],
                    replace_existing=True,
                    max_instances=1,
                    coalesce=True,
                    misfire_grace_time=900,
                )
            except Exception:
                logger.error("invalid automation schedule owner=%s job=%s", row["owner_user_id"], row["id"])
        for job in self.scheduler.get_jobs():
            if job.id.startswith("tenant-") and job.id not in expected:
                self.scheduler.remove_job(job.id)

    def _enqueue_scheduled(self, owner_user_id: int, job_id: int, kind: str) -> None:
        scheduled_for = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        try:
            enqueue = self.repository.enqueue_run(owner_user_id, kind, scheduled_for, source="schedule", job_id=job_id)
            if not enqueue["created"]:
                logger.info("automation request already active owner=%s job=%s", owner_user_id, job_id)
        except Exception:
            logger.exception("failed to enqueue owner=%s job=%s", owner_user_id, job_id)

    def _next_due_owner(self) -> int | None:
        now = datetime.now(timezone.utc).isoformat()
        with self.repository.connection() as conn:
            row = conn.execute(
                """SELECT owner_user_id FROM automation_run_requests
                   WHERE scheduled_for<=? AND (status='queued' OR
                   (status IN ('claimed','running') AND lease_expires_at<=?))
                   ORDER BY scheduled_for,id LIMIT 1""",
                (now, now),
            ).fetchone()
        return row[0] if row else None

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            owner = self._next_due_owner()
            if owner is None:
                self._stop.wait(2)
                continue
            claimed = self.repository.claim_next_run(owner, self.worker_id, lease_seconds=7200)
            if not claimed:
                self._stop.wait(0.2)
                continue
            run_id = None
            try:
                run_id = self.repository.start_claimed_run(owner, claimed["id"], self.worker_id, claimed["claim_token"])
                if claimed["kind"] == "update":
                    summary = self._run_update(owner, claimed, run_id)
                    result = summary.get("result")
                    status = result if result in {"success", "partial", "no_changes", "failed"} else "failed"
                elif claimed["kind"] == "email":
                    status, summary = self._run_email(owner, claimed.get("job_id"), run_id)
                else:
                    raise ValueError("unsupported automation kind")
                self.repository.finish_run(owner, run_id, status, summary, claim_token=claimed["claim_token"])
            except Exception as exc:
                logger.error(
                    "automation run failed owner=%s request=%s type=%s", owner, claimed["id"], type(exc).__name__
                )
                if run_id is not None:
                    self.repository.finish_run(
                        owner,
                        run_id,
                        "failed",
                        {"error": "automation_failed"},
                        claim_token=claimed["claim_token"],
                    )
                else:
                    self.repository.fail_run_request(owner, claimed["id"], {"error": "automation_failed"})

    def _run_update(self, owner: int, claimed: dict, run_id: int) -> dict:
        payload = claimed.get("payload") or {"update_scope": "all"}
        update_scope = payload.get("update_scope", "all")
        if update_scope == "all":
            summary = self.tracking.track_journals(TenantRunContext(owner, owner, claimed["id"], run_id), headless=True)
            summary["update_scope"] = "all"
            return summary
        if update_scope not in {"starred", "working"}:
            raise ValueError("invalid update scope")
        requested_ids = payload.get("journal_config_ids")
        if not isinstance(requested_ids, list):
            raise ValueError(f"invalid {update_scope} update payload")
        configs = self.repository.journal_configs_by_ids(owner, requested_ids, enabled_only=True)
        if configs:
            summary = self.tracking.track_journals(
                TenantRunContext(owner, owner, claimed["id"], run_id),
                journal_configs=configs,
                headless=True,
            )
        else:
            summary = {
                "success": True,
                "result": "no_changes",
                "error": None,
                "total_manuscripts": 0,
                "attempted_journals": 0,
                "successful_journals": 0,
                "failed_journals": 0,
                "results": {},
                "journal_results": [],
                "reason": f"no_enabled_{update_scope}_journals",
            }
        summary.update(
            {
                "update_scope": update_scope,
                "requested_journals": len(requested_ids),
                "skipped_disabled_journals": len(requested_ids) - len(configs),
            }
        )
        return summary

    def _run_email(self, owner: int, job_id: int | None, run_id: int) -> tuple[str, dict]:
        if job_id is None:
            raise ValueError("email run requires a job")
        job = self.repository.get_automation_job(owner, job_id)
        smtp = self.repository.get_user_smtp(owner)
        if not job or job["kind"] != "email":
            raise LookupError("email job not found")
        if not smtp or not smtp.get("credential_ref"):
            raise RuntimeError("personal SMTP is not configured")
        recipients = job.get("recipients") or []
        if not recipients:
            raise RuntimeError("email recipients are not configured")
        payload = job.get("payload") or {}
        scope = payload.get("manuscript_scope", "all")
        items = self.repository.email_items(owner, scope)
        if payload.get("delivery_policy") == "changes_only":
            with self.repository.connection() as conn:
                prior = {
                    row["original_id"]: row["state_fingerprint"]
                    for row in conn.execute(
                        "SELECT original_id,state_fingerprint FROM automation_email_manuscript_state WHERE owner_user_id=? AND job_id=?",
                        (owner, job_id),
                    )
                }
            items = [item for item in items if prior.get(item["original_id"]) != manuscript_fingerprint(item)]
        if not items:
            return "no_changes", {"item_count": 0}

        unread_ids = self.repository.unread_status_ids(owner, items)
        new_items = [item for item in items if item["original_id"] in unread_ids]
        raw_ack = random_token() if new_items else None
        expires_at = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat() if raw_ack else None
        message_id = make_msgid(domain="manuscript-desk.local")
        delivery_id = self.repository.create_email_delivery(
            owner,
            job_id,
            new_items,
            message_id,
            token_hash(raw_ack) if raw_ack else None,
            expires_at,
            run_id,
        )
        try:
            self._send_digest(smtp, recipients, items, new_items, raw_ack, message_id)
        except Exception:
            self.repository.mark_email_delivery(owner, delivery_id, "failed")
            raise
        self.repository.mark_email_delivery(owner, delivery_id, "sent")
        with self.repository.connection(immediate=True) as conn:
            for item in items:
                conn.execute(
                    """INSERT INTO automation_email_manuscript_state(owner_user_id,job_id,original_id,state_fingerprint)
                       VALUES(?,?,?,?) ON CONFLICT(owner_user_id,job_id,original_id) DO UPDATE SET
                       state_fingerprint=excluded.state_fingerprint,updated_at=CURRENT_TIMESTAMP""",
                    (owner, job_id, item["original_id"], manuscript_fingerprint(item)),
                )
        return "success", {
            "item_count": len(items),
            "new_status_count": len(new_items),
            "recipient_count": len(recipients),
        }

    @staticmethod
    def _status_color(status: str) -> tuple[str, str]:
        normalized = (status or "").casefold()
        if "accept" in normalized:
            return "#166534", "#dcfce7"
        if "reject" in normalized:
            return "#991b1b", "#fee2e2"
        if "revision" in normalized or "revise" in normalized:
            return "#92400e", "#fef3c7"
        return "#4338ca", "#e0e7ff"

    def _send_digest(
        self,
        config: dict,
        recipients: list[str],
        items: list[dict],
        new_items: list[dict],
        ack_token: str | None,
        message_id: str,
    ) -> None:
        password = self.secret_store.read(config["credential_ref"])
        message = EmailMessage()
        message["Subject"] = f"Manuscript Desk · {len(new_items)}/{len(items)} 篇新状态"
        message["From"] = (
            f"{config['sender_name']} <{config['sender_address']}>"
            if config.get("sender_name")
            else config["sender_address"]
        )
        message["To"] = ", ".join(recipients)
        message["Message-ID"] = message_id

        new_ids = {item["original_id"] for item in new_items}
        lines = [
            "Manuscript Desk 稿件状态摘要",
            f"本次共 {len(items)} 篇稿件，其中 {len(new_items)} 篇有未读新状态。",
            "",
        ]
        for item in items:
            marker = "[新状态] " if item["original_id"] in new_ids else ""
            lines.extend(
                [
                    f"{marker}{item.get('title') or item.get('manuscript_id') or item['original_id']}",
                    f"期刊：{item.get('journal', '')}",
                    f"稿件编号：{item.get('manuscript_id') or item['original_id']}",
                    f"当前状态：{item.get('detailed_status') or item.get('status', '')}",
                    "",
                ]
            )
        if ack_token:
            lines.extend(
                [
                    "邮件中有未读新状态。点击以下链接，将这些新状态标记为已读：",
                    f"{self.public_base_url}/starred/email-ack#token={ack_token}",
                    "链接有效期为一周，且只能使用一次。",
                ]
            )
        message.set_content("\n".join(lines))

        cards = []
        for item in items:
            is_new = item["original_id"] in new_ids
            status = str(item.get("detailed_status") or item.get("status") or "未知状态")
            status_text, status_bg = self._status_color(status)
            new_badge = (
                '<span style="display:inline-block;margin-left:8px;padding:3px 8px;'
                "border-radius:999px;background:#7c3aed;color:#ffffff;font-size:12px;"
                'font-weight:700;vertical-align:middle;">新状态</span>'
                if is_new
                else ""
            )
            cards.append(f"""
              <tr><td style="padding:0 0 12px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
                       style="border:1px solid {"#c4b5fd" if is_new else "#e7e1d7"};border-radius:14px;background:{"#faf8ff" if is_new else "#ffffff"};">
                  <tr><td style="padding:18px 20px;">
                    <div style="font-size:16px;line-height:1.45;font-weight:700;color:#292524;">
                      {html.escape(str(item.get("title") or item.get("manuscript_id") or item["original_id"]))}{new_badge}
                    </div>
                    <div style="margin-top:9px;font-size:13px;line-height:1.6;color:#78716c;">
                      {html.escape(str(item.get("journal") or ""))}<br>
                      {html.escape(str(item.get("manuscript_id") or item["original_id"]))}
                    </div>
                    <div style="margin-top:13px;">
                      <span style="display:inline-block;padding:5px 10px;border-radius:999px;background:{status_bg};color:{status_text};font-size:13px;font-weight:700;">
                        {html.escape(status)}
                      </span>
                    </div>
                  </td></tr>
                </table>
              </td></tr>""")

        ack_block = ""
        if ack_token:
            ack_url = f"{self.public_base_url}/starred/email-ack#token={ack_token}"
            ack_block = f"""
              <tr><td style="padding:8px 28px 30px;">
                <table role="presentation" width="100%" cellspacing="0" cellpadding="0"
                       style="border-radius:14px;background:#f2efff;">
                  <tr><td style="padding:22px;text-align:center;">
                    <div style="font-size:15px;line-height:1.7;color:#4c4668;">
                      邮件中有 <strong>{len(new_items)}</strong> 篇稿件出现未读新状态。<br>
                      点击下方按钮，将这些新状态标记为已读。
                    </div>
                    <div style="margin-top:18px;">
                      <a href="{html.escape(ack_url, quote=True)}"
                         style="display:inline-block;padding:12px 22px;border-radius:10px;background:#5d4fc4;color:#ffffff;text-decoration:none;font-size:15px;font-weight:700;">
                        标记为已读
                      </a>
                    </div>
                    <div style="margin-top:13px;font-size:12px;line-height:1.6;color:#78716c;">
                      链接有效期为一周，且只能使用一次。若稿件状态之后再次变化，新状态不会被误标记为已读。
                    </div>
                  </td></tr>
                </table>
              </td></tr>"""

        html_body = f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f1e8;color:#292524;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Microsoft YaHei',Arial,sans-serif;">
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f5f1e8;">
    <tr><td align="center" style="padding:28px 12px;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:680px;background:#fffdf8;border:1px solid #e7e1d7;border-radius:20px;overflow:hidden;">
        <tr><td style="padding:30px 28px 22px;background:#352d63;color:#ffffff;">
          <div style="font-size:12px;font-weight:800;letter-spacing:.14em;color:#d8d1ff;">MANUSCRIPT DESK</div>
          <div style="margin-top:8px;font-size:26px;line-height:1.25;font-weight:800;">稿件状态摘要</div>
          <div style="margin-top:10px;font-size:14px;line-height:1.7;color:#eeeaff;">
            本次共 {len(items)} 篇稿件，其中 <strong>{len(new_items)}</strong> 篇有未读新状态。
          </div>
        </td></tr>
        <tr><td style="padding:24px 28px 12px;">
          <table role="presentation" width="100%" cellspacing="0" cellpadding="0">{"".join(cards)}</table>
        </td></tr>
        {ack_block}
        <tr><td style="padding:18px 28px 24px;border-top:1px solid #ebe5da;font-size:12px;line-height:1.7;color:#8a8175;text-align:center;">
          此邮件由 Manuscript Desk 自动发送。
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
        message.add_alternative(html_body, subtype="html")
        context = ssl.create_default_context()
        if config["security_mode"] == "ssl":
            client = smtplib.SMTP_SSL(config["host"], int(config["port"]), timeout=20, context=context)
        else:
            client = smtplib.SMTP(config["host"], int(config["port"]), timeout=20)
        try:
            if config["security_mode"] == "starttls":
                client.ehlo()
                client.starttls(context=context)
                client.ehlo()
            if config.get("username"):
                client.login(config["username"], password)
            client.send_message(message, to_addrs=recipients)
        finally:
            try:
                client.quit()
            except Exception:
                client.close()
