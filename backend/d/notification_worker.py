"""D-S09：持久领取、单收件人投递和有限重试；SMTP 不在数据库事务内执行。"""
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import logging
from uuid import uuid4

from sqlalchemy import select

from backend.d.mail_transport import MailResult, load_mail_config, send_email
from backend.d.models import Notification, NotificationDelivery, User
from backend.d.notifications import _mailbox
from backend.shared.clock import utc_now
from backend.shared.db import session_factory

logger = logging.getLogger(__name__)
LEASE_SECONDS = 120
BATCH_SIZE = 10


@dataclass(frozen=True)
class Claim:
    notification_id: str
    user_id: str
    token: str
    request_id: str
    recipient: str
    title: str
    body: str
    created_at: datetime
    delivery_id: str


def _log(row, reason):
    # 不记录正文、邮箱、服务器原始响应或异常内容。
    logger.warning('D S09 notificationId=%r requestId=%r reason=%s',
                   row.notification_id, row.request_id, reason)


def expire_leases():
    """租约过期也不能证明 SMTP 未接受；绝不重新领取 SENDING。"""
    now = utc_now()
    with session_factory('d')() as db, db.begin():
        rows = db.scalars(select(NotificationDelivery).where(
            NotificationDelivery.status == 'SENDING',
            NotificationDelivery.lease_expires_at <= now,
        ).order_by(NotificationDelivery.lease_expires_at).limit(BATCH_SIZE)
            .with_for_update(skip_locked=True)).all()
        for row in rows:
            row.status, row.reason = 'UNKNOWN', 'WORKER_LEASE_EXPIRED'
            row.attempt_token = row.lease_expires_at = None
            notice = db.get(Notification, row.notification_id)
            _log(notice, row.reason)
    return len(rows)


def claim_one():
    """返回已提交的领取快照；提交失败时调用者绝不进入 SMTP。"""
    now = utc_now()
    claim = None
    with session_factory('d')() as db, db.begin():
        row = db.scalar(select(NotificationDelivery).where(
            NotificationDelivery.status == 'PENDING',
            NotificationDelivery.next_attempt_at <= now,
        ).order_by(NotificationDelivery.next_attempt_at, NotificationDelivery.notification_id,
                   NotificationDelivery.recipient_order).limit(1).with_for_update(skip_locked=True))
        if row is None:
            return None
        notice = db.get(Notification, row.notification_id)
        user = db.get(User, row.recipient_user_id)
        recipient, address_reason = _mailbox(row.recipient_email)
        reason = None
        if user is None or user.account_status != 'ACTIVE':
            reason = 'ACCOUNT_DISABLED'
        elif address_reason != 'QUEUED':
            reason = address_reason
        elif row.attempt_count >= notice.retry_limit + 1:
            reason = 'RETRY_LIMIT_EXHAUSTED'
        if reason:
            row.status, row.reason, row.next_attempt_at = 'FAILED', reason, None
            _log(notice, reason)
        else:
            row.status, row.reason = 'SENDING', 'SMTP_IN_PROGRESS'
            row.attempt_count += 1
            row.last_attempt_at, row.next_attempt_at = now, None
            row.attempt_token = uuid4().hex
            row.lease_expires_at = now + timedelta(seconds=LEASE_SECONDS)
            # 不把用户标识放进邮件头；同一投递在有限重试间保留 Message-ID。
            delivery_id = hashlib.sha256(
                f'{row.notification_id}\0{row.recipient_user_id}'.encode('utf-8')).hexdigest()
            claim = Claim(row.notification_id, row.recipient_user_id, row.attempt_token,
                          notice.request_id, recipient, notice.title, notice.body,
                          notice.created_at, delivery_id)
    return claim


def finish(claim, result):
    """只允许当前令牌完成；过期或已完成的结果不能覆盖持久状态。"""
    now = utc_now()
    with session_factory('d')() as db, db.begin():
        row = db.scalar(select(NotificationDelivery).where(
            NotificationDelivery.notification_id == claim.notification_id,
            NotificationDelivery.recipient_user_id == claim.user_id,
        ).with_for_update())
        if row is None or row.status != 'SENDING' or row.attempt_token != claim.token:
            return False
        notice = db.get(Notification, row.notification_id)
        if row.lease_expires_at <= now:
            status, reason = 'UNKNOWN', 'WORKER_LEASE_EXPIRED'
        elif result.outcome == 'SUBMITTED':
            status, reason = 'SUBMITTED', result.reason
        elif result.outcome == 'TEMPORARY_FAILURE' and row.attempt_count <= notice.retry_limit:
            status, reason = 'PENDING', result.reason
            row.next_attempt_at = max(now, row.last_attempt_at) + timedelta(
                seconds=min(300, 5 * 2 ** (row.attempt_count - 1)))
        elif result.outcome in {'TEMPORARY_FAILURE', 'PERMANENT_FAILURE'}:
            status, reason = 'FAILED', result.reason
        else:
            status, reason = 'UNKNOWN', result.reason
        row.status, row.reason = status, reason
        row.attempt_token = row.lease_expires_at = None
        if status != 'PENDING':
            row.next_attempt_at = None
        if status != 'SUBMITTED':
            _log(notice, reason)
    return True


def run_batch():
    expire_leases()
    # 配置先校验再领取；无效配置保留 PENDING，并由 tick 上报 workerError。
    config = load_mail_config()
    count = 0
    for _ in range(BATCH_SIZE):
        claim = claim_one()
        if claim is None:
            break
        try:
            result = send_email(delivery_id=claim.delivery_id, recipient=claim.recipient,
                                title=claim.title, body=claim.body, created_at=claim.created_at,
                                config=config)
        except Exception as exc:
            _log(claim, f'TRANSPORT_EXCEPTION_{type(exc).__name__}')
            # 未知实现异常也不能安全重试；完成写入失败则由租约到期兜底。
            finish(claim, MailResult('UNKNOWN', 'TRANSPORT_EXCEPTION'))
            raise RuntimeError('D S09 transport failed; inspect controlled worker log') from None
        try:
            finish(claim, result)
        except Exception as exc:
            _log(claim, f'FINALIZE_EXCEPTION_{type(exc).__name__}')
            raise RuntimeError('D S09 result persistence failed; preserve SENDING') from None
        count += 1
    return count


async def tick():
    try:
        # 网络及数据库是同步库，放在线程中以免阻塞 HTTP 事件循环。
        await asyncio.to_thread(run_batch)
    except Exception as exc:
        logger.error('D S09 requestId=- reason=WORKER_EXCEPTION_%s', type(exc).__name__)
        raise RuntimeError('D notification worker failed') from None
