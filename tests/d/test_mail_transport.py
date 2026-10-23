"""SMTP 边界：只模拟外部 SMTP 对话，真实构造 MIME 和判定失败结果。"""
from dataclasses import replace
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
import smtplib
import ssl

import pytest

from backend.d import mail_transport as mail

CONFIG = mail.MailConfig('127.0.0.1', 1025, 'campusflow@example.test', 'none')
ARGS = dict(delivery_id='stable-123', recipient='person@example.test', title='维修通知',
            body='第一行\n.第二行', created_at=datetime(2026, 1, 1, tzinfo=timezone.utc))


class SMTPPeer:
    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []
        self.wire = None

    def call(self, stage):
        self.calls.append(stage)
        response = self.replies.get(stage, (250, b'OK'))
        if isinstance(response, Exception):
            raise response
        return response

    def ehlo_or_helo_if_needed(self):
        return self.call('HELLO')

    def starttls(self, **kwargs):
        assert kwargs['context'].verify_mode == ssl.CERT_REQUIRED
        return self.call('TLS')

    def login(self, username, password):
        assert username == 'name' and password == 'secret'
        return self.call('AUTH')

    def mail(self, sender):
        assert sender == CONFIG.sender
        return self.call('MAIL')

    def rcpt(self, recipient):
        assert recipient == ARGS['recipient']
        return self.call('RCPT')

    def data(self, wire):
        self.wire = wire
        return self.call('DATA')

    def close(self):
        self.call('CLOSE')


@pytest.fixture
def smtp(monkeypatch):
    peer = SMTPPeer()
    def connect(**kwargs):
        assert kwargs['timeout'] == 5
        assert kwargs['local_hostname'] == 'campusflow.local'
        peer.call('CONNECT')
        return peer
    monkeypatch.setattr(mail.smtplib, 'SMTP', connect)
    monkeypatch.setattr(mail.smtplib, 'SMTP_SSL', connect)
    return peer


def test_mime_chinese_plain_text_one_recipient_and_stable_id():
    first = mail.build_message(**ARGS, sender=CONFIG.sender)
    assert first == mail.build_message(**ARGS, sender=CONFIG.sender)
    message = BytesParser(policy=policy.default).parsebytes(first)
    assert str(message['Subject']) == ARGS['title']
    assert str(message['To']) == ARGS['recipient']
    assert str(message['From']) == CONFIG.sender
    assert str(message['Message-ID']) == '<stable-123@campusflow.local>'
    assert message.get_content().replace('\r\n', '\n') == ARGS['body'] + '\n'
    assert message.get_content_type() == 'text/plain'
    assert message.get_content_charset() == 'utf-8'
    assert message['Bcc'] is None and message['Cc'] is None
    assert b'\n' not in first.replace(b'\r\n', b'')


@pytest.mark.parametrize('field,value', [
    ('delivery_id', 'x\r\nBcc:x@y.test'), ('recipient', 'a@b.test,c@d.test'),
    ('recipient', 'a\n@b.test'), ('title', 'x\nBcc:x@y.test'), ('title', ''),
    ('body', ' '), ('title', 'x' * 2001), ('body', 'x' * 2001),
    ('created_at', datetime(2026, 1, 1)),
])
def test_invalid_message_never_connects(smtp, field, value):
    with pytest.raises(ValueError):
        mail.send_email(**{**ARGS, field: value}, config=CONFIG)
    assert smtp.calls == []


@pytest.mark.parametrize('stage,response,outcome', [
    ('CONNECT', OSError('raw secret'), 'TEMPORARY_FAILURE'),
    ('CONNECT', smtplib.SMTPConnectError(421, b'raw secret'), 'TEMPORARY_FAILURE'),
    ('HELLO', smtplib.SMTPHeloError(550, b'raw secret'), 'PERMANENT_FAILURE'),
    ('MAIL', (451, b'raw secret'), 'TEMPORARY_FAILURE'),
    ('MAIL', (550, b'raw secret'), 'PERMANENT_FAILURE'),
    ('RCPT', (450, b'raw secret'), 'TEMPORARY_FAILURE'),
    ('RCPT', (550, b'raw secret'), 'PERMANENT_FAILURE'),
    ('RCPT', OSError('raw secret'), 'TEMPORARY_FAILURE'),
    ('DATA', (451, b'raw secret'), 'TEMPORARY_FAILURE'),
    ('DATA', (550, b'raw secret'), 'PERMANENT_FAILURE'),
    ('DATA', smtplib.SMTPDataError(451, b'raw secret'), 'TEMPORARY_FAILURE'),
    ('DATA', smtplib.SMTPDataError(554, b'raw secret'), 'PERMANENT_FAILURE'),
    ('DATA', (250, b'OK'), 'SUBMITTED'),
    ('DATA', OSError('raw secret'), 'UNKNOWN'),
    ('DATA', TimeoutError('raw secret'), 'UNKNOWN'),
    ('DATA', smtplib.SMTPServerDisconnected('raw secret'), 'UNKNOWN'),
    ('DATA', smtplib.SMTPResponseException(500, b'Line too long'), 'UNKNOWN'),
    ('DATA', (-1, b'bad reply'), 'UNKNOWN'),
])
def test_failure_boundary_and_no_internal_retry(smtp, stage, response, outcome):
    smtp.replies[stage] = response
    result = mail.send_email(**ARGS, config=CONFIG)
    assert result.outcome == outcome
    assert 'raw secret' not in result.reason
    assert smtp.calls.count('CONNECT') == 1
    assert smtp.calls.count('DATA') <= 1
    if stage not in {'DATA', 'CONNECT'}:
        assert 'DATA' not in smtp.calls


def test_close_failure_cannot_undo_acceptance(smtp):
    smtp.replies['CLOSE'] = OSError('closed')
    smtp.replies['RCPT'] = (251, b'forwarded')
    result = mail.send_email(**ARGS, config=CONFIG)
    assert result == mail.MailResult('SUBMITTED', 'SMTP_ACCEPTED')
    assert smtp.calls == ['CONNECT', 'HELLO', 'MAIL', 'RCPT', 'DATA', 'CLOSE']


@pytest.mark.parametrize('security', ['starttls', 'ssl'])
def test_secure_auth_before_envelope(smtp, security):
    config = replace(CONFIG, security=security, username='name', password='secret')
    assert 'secret' not in repr(config)
    assert mail.send_email(**ARGS, config=config).outcome == 'SUBMITTED'
    assert smtp.calls.index('AUTH') < smtp.calls.index('MAIL')
    assert ('TLS' in smtp.calls) == (security == 'starttls')


@pytest.mark.parametrize('response,reason', [
    (smtplib.SMTPNotSupportedError('raw secret'), 'SMTP_FEATURE_UNSUPPORTED'),
    (ssl.SSLCertVerificationError('raw secret'), 'SMTP_TLS_CERTIFICATE_INVALID'),
])
def test_tls_failure_never_sends_credentials_or_body(smtp, response, reason):
    smtp.replies['TLS'] = response
    result = mail.send_email(**ARGS, config=replace(CONFIG, security='starttls', username='name', password='secret'))
    assert result == mail.MailResult('PERMANENT_FAILURE', reason)
    assert 'AUTH' not in smtp.calls and 'MAIL' not in smtp.calls


@pytest.fixture
def smtp_env(monkeypatch):
    monkeypatch.setattr(mail, 'load_dotenv', lambda *a, **kw: None)
    for key, value in dict(APP_ENV='test', SMTP_HOST='127.0.0.1', SMTP_PORT='1025',
                           SMTP_FROM=CONFIG.sender, SMTP_USERNAME='', SMTP_PASSWORD='').items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv('SMTP_SECURITY', raising=False)


def test_local_mailpit_configuration(smtp_env):
    assert mail.load_mail_config() == CONFIG


@pytest.mark.parametrize('key,value', [
    ('SMTP_HOST', ''), ('SMTP_HOST', 'x\nhost'), ('SMTP_PORT', 'bad'),
    ('SMTP_PORT', '0'), ('SMTP_PORT', '65536'), ('SMTP_FROM', 'x'),
    ('SMTP_SECURITY', 'bad'), ('SMTP_USERNAME', 'name'), ('SMTP_PASSWORD', 'secret'),
])
def test_invalid_configuration(smtp_env, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(mail.MailConfigurationError):
        mail.load_mail_config()


def test_nonlocal_requires_tls_and_plaintext_auth_is_rejected(smtp_env, monkeypatch):
    monkeypatch.setenv('SMTP_HOST', 'smtp.example.test')
    assert mail.load_mail_config().security == 'starttls'
    monkeypatch.setenv('SMTP_SECURITY', 'none')
    with pytest.raises(mail.MailConfigurationError):
        mail.load_mail_config()
    monkeypatch.setenv('SMTP_HOST', '127.0.0.1')
    monkeypatch.setenv('SMTP_USERNAME', 'name')
    monkeypatch.setenv('SMTP_PASSWORD', 'secret')
    with pytest.raises(mail.MailConfigurationError):
        mail.load_mail_config()
