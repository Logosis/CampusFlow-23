"""D 内部单收件人 SMTP 通道；不修改数据库、不自行重试、不注册后台任务。"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import format_datetime
import os
import re
import smtplib
import ssl

from dotenv import load_dotenv

from backend.d.notifications import _mailbox
from backend.shared.settings import ROOT


class MailConfigurationError(ValueError):
    pass


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    sender: str
    security: str
    username: str = field(default='', repr=False)
    password: str = field(default='', repr=False)


@dataclass(frozen=True)
class MailResult:
    # TEMPORARY_FAILURE/PERMANENT_FAILURE 是内部结果，由后续队列决定公开状态。
    outcome: str
    reason: str


def load_mail_config():
    load_dotenv(ROOT / '.env', override=False)
    host = os.getenv('SMTP_HOST', '').strip()
    try:
        port = int(os.getenv('SMTP_PORT', '1025'))
    except ValueError:
        raise MailConfigurationError('SMTP_PORT必须是有效端口') from None
    sender, valid = _mailbox(os.getenv('SMTP_FROM'))
    if not host or any(c.isspace() or ord(c) < 32 for c in host) or not 1 <= port <= 65535 or valid != 'QUEUED':
        raise MailConfigurationError('SMTP地址、端口或发件人配置不合法')
    local_test = os.getenv('APP_ENV') == 'test' and host in {'127.0.0.1', 'localhost', '::1', 'mailpit'}
    security = os.getenv('SMTP_SECURITY') or ('none' if local_test else 'starttls')
    username, password = os.getenv('SMTP_USERNAME', ''), os.getenv('SMTP_PASSWORD', '')
    if security not in {'none', 'starttls', 'ssl'}:
        raise MailConfigurationError('SMTP_SECURITY必须为none、starttls或ssl')
    if security == 'none' and (not local_test or username or password):
        raise MailConfigurationError('仅无认证的本地测试邮件服务允许明文SMTP')
    if bool(username) != bool(password):
        raise MailConfigurationError('SMTP用户名与密码必须一起配置')
    return MailConfig(host, port, sender, security, username, password)


def build_message(*, delivery_id, recipient, title, body, created_at, sender):
    """稳定Message-ID仅便于追踪，不承诺SMTP服务器按此去重。"""
    if not isinstance(delivery_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,180}', delivery_id):
        raise ValueError('Invalid internal delivery identifier')
    if _mailbox(recipient)[1] != 'QUEUED' or _mailbox(sender)[1] != 'QUEUED':
        raise ValueError('Invalid single mailbox')
    if not isinstance(title, str) or not title.strip() or len(title) > 2000 or '\r' in title or '\n' in title:
        raise ValueError('Invalid message title')
    if not isinstance(body, str) or not body.strip() or len(body) > 2000:
        raise ValueError('Invalid message body')
    if not isinstance(created_at, datetime) or created_at.tzinfo is None or created_at.utcoffset() is None:
        raise ValueError('Message time requires timezone')
    message = EmailMessage(policy=SMTP_POLICY)
    message['From'] = sender
    message['To'] = recipient
    message['Subject'] = title
    message['Date'] = format_datetime(created_at.astimezone(timezone.utc), usegmt=True)
    message['Message-ID'] = f'<{delivery_id}@campusflow.local>'
    message.set_content(body, charset='utf-8', cte='quoted-printable')
    return message.as_bytes()


def _rejected(code, stage, *, uncertain=False):
    if 400 <= code <= 499:
        return MailResult('TEMPORARY_FAILURE', f'SMTP_{stage}_{code}')
    if 500 <= code <= 599:
        return MailResult('PERMANENT_FAILURE', f'SMTP_{stage}_{code}')
    return MailResult('UNKNOWN' if uncertain else 'PERMANENT_FAILURE', 'SMTP_PROTOCOL_ERROR')


def send_email(*, delivery_id, recipient, title, body, created_at, config=None):
    """一次调用仅尝试一个收件人一次；只返回受控原因，不返回服务器原始文本。"""
    config = load_mail_config() if config is None else config
    wire = build_message(delivery_id=delivery_id, recipient=recipient, title=title, body=body,
                         created_at=created_at, sender=config.sender)
    client = None
    data_started = False
    stage = 'CONNECT'
    try:
        kwargs = dict(host=config.host, port=config.port, timeout=5, local_hostname='campusflow.local')
        if config.security == 'ssl':
            client = smtplib.SMTP_SSL(**kwargs, context=ssl.create_default_context())
        elif config.security in {'none', 'starttls'}:
            client = smtplib.SMTP(**kwargs)
        else:
            raise MailConfigurationError('Unsupported SMTP security mode')
        stage = 'HELLO'
        client.ehlo_or_helo_if_needed()
        if config.security == 'starttls':
            stage = 'TLS'
            client.starttls(context=ssl.create_default_context())
            client.ehlo_or_helo_if_needed()
        if config.username:
            stage = 'AUTH'
            client.login(config.username, config.password)
        stage = 'MAIL'
        code, _ = client.mail(config.sender)
        if code != 250:
            return _rejected(code, stage)
        stage = 'RCPT'
        code, _ = client.rcpt(recipient)
        if code not in {250, 251}:
            return _rejected(code, stage)
        stage = 'DATA'
        # 从发起DATA开始对断线保守处理；不能确定是否已经接收正文时不自动重发。
        data_started = True
        code, _ = client.data(wire)
        if code == 250:
            return MailResult('SUBMITTED', 'SMTP_ACCEPTED')
        return _rejected(code, stage, uncertain=True)
    except smtplib.SMTPDataError as exc:
        # Python 3.12 data()只在未获354时抛此异常，正文尚未发送。
        return _rejected(exc.smtp_code, 'DATA')
    except ssl.SSLCertVerificationError:
        return MailResult('PERMANENT_FAILURE', 'SMTP_TLS_CERTIFICATE_INVALID')
    except smtplib.SMTPNotSupportedError:
        return MailResult('PERMANENT_FAILURE', 'SMTP_FEATURE_UNSUPPORTED')
    except smtplib.SMTPResponseException as exc:
        # DATA中读取损坏/超长响应也可能抛SMTPResponseException(500)，不能当明确拒绝。
        if data_started:
            return MailResult('UNKNOWN', 'SMTP_RESPONSE_UNKNOWN')
        return _rejected(exc.smtp_code, stage)
    except (OSError, smtplib.SMTPException):
        return MailResult('UNKNOWN', 'SMTP_RESULT_UNKNOWN') if data_started else MailResult(
            'TEMPORARY_FAILURE', 'SMTP_CONNECTION_FAILED')
    finally:
        if client is not None:
            try:
                # 已收到最终250后，连接关闭失败不改变已接受的结果。
                client.close()
            except OSError:
                pass
