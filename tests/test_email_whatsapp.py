import json
import os
import plistlib
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
import email_whatsapp as ew  # noqa: E402


class Resp:
    def __init__(self, text="Message queued", status_code=200):
        self.text, self.status_code = text, status_code


def doc(mid, channel="INBOX", author="boss@x.co", title="Review", content="Body"):
    return types.SimpleNamespace(
        metadata={"message_id": mid}, doc_id=f"gmail:{mid}", channel=channel,
        author=author, title=title, content=content,
    )


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(ew, "STATE_FILE", tmp_path / "state.json")
    monkeypatch.setattr(ew, "SETTINGS_FILE", tmp_path / "settings.env")
    (tmp_path / "settings.env").write_text(
        "CALLMEBOT_PHONE=254700\nCALLMEBOT_APIKEY=secret\nCALLMEBOT_DELAY_SECONDS=0\n"
    )
    monkeypatch.setattr(ew, "summarise", lambda emails: "Boss: see http://evil.example.com/x or evil.com")
    sent = []

    def fake_get(url, params, timeout):
        sent.append(params)
        return Resp()

    monkeypatch.setattr(ew.httpx, "get", fake_get)
    return types.SimpleNamespace(path=tmp_path, sent=sent)


def use_docs(monkeypatch, docs, connected=True):
    class Connector:
        def is_connected(self):
            return connected

        def sync(self, since=None):
            self.since = since
            return iter(docs)

    module = types.ModuleType("openjarvis.connectors.gmail")
    module.GmailConnector = Connector
    monkeypatch.setitem(sys.modules, "openjarvis", types.ModuleType("openjarvis"))
    monkeypatch.setitem(sys.modules, "openjarvis.connectors", types.ModuleType("openjarvis.connectors"))
    monkeypatch.setitem(sys.modules, "openjarvis.connectors.gmail", module)


def test_counts_spam_and_skips_sent_and_trash(app, monkeypatch):
    use_docs(monkeypatch, [doc("a"), doc("b", "SPAM"), doc("c", "SENT"), doc("d", "TRASH"), doc("e", "DRAFT")])
    ew.run()
    assert "2 new email(s), 1 in spam" in app.sent[0]["text"]
    assert set(json.loads(ew.STATE_FILE.read_text())["seen"]) == {"a", "b"}


def test_removes_links_from_messages(app, monkeypatch):
    use_docs(monkeypatch, [doc("a")])
    ew.run()
    assert "evil" not in app.sent[0]["text"] and "http" not in app.sent[0]["text"]


def test_keeps_email_addresses_readable():
    assert ew.strip_links("boss@x.co: hello") == "boss@x.co: hello"


def test_does_not_repeat_emails(app, monkeypatch):
    use_docs(monkeypatch, [doc("a")])
    ew.run()
    ew.run()
    assert "no new emails" in app.sent[1]["text"]


def test_sends_sender_list_when_model_fails(app, monkeypatch):
    use_docs(monkeypatch, [doc("a", author="new@x.co", title="Invoice")])

    def fail(_):
        raise RuntimeError("model down")

    monkeypatch.setattr(ew, "summarise", fail)
    ew.run()
    assert "AI summary unavailable" in app.sent[0]["text"]
    assert "new@x.co: Invoice" in app.sent[0]["text"]


def test_failed_send_is_retried_next_time_without_leaking_key(app, monkeypatch):
    use_docs(monkeypatch, [doc("a")])
    monkeypatch.setattr(ew.httpx, "get", lambda *a, **k: Resp("APIKey is invalid"))
    with pytest.raises(RuntimeError) as err:
        ew.run()
    assert "secret" not in str(err.value)
    assert not ew.STATE_FILE.exists()
    sent = []
    monkeypatch.setattr(ew.httpx, "get", lambda url, params, timeout: sent.append(params) or Resp())
    ew.run()
    assert "1 new email(s)" in sent[0]["text"]


def test_gmail_failure_sends_alert_and_does_not_advance(app, monkeypatch):
    use_docs(monkeypatch, [], connected=False)
    with pytest.raises(RuntimeError):
        ew.run()
    assert "Could not read Gmail" in app.sent[0]["text"]
    assert not ew.STATE_FILE.exists()


def test_says_when_more_emails_exist_than_the_cap(app, monkeypatch):
    monkeypatch.setattr(ew, "MAX_EMAILS", 2)
    use_docs(monkeypatch, [doc("a"), doc("b"), doc("c")])
    ew.run()
    assert "newest 2 email(s) shown, more in Gmail" in app.sent[0]["text"]


def test_placeholder_settings_stop_the_run(app, monkeypatch):
    ew.SETTINGS_FILE.write_text("CALLMEBOT_PHONE=254700\nCALLMEBOT_APIKEY=PASTE_API_KEY_HERE\n")
    with pytest.raises(SystemExit) as err:
        ew.run()
    assert "CALLMEBOT_APIKEY" in str(err.value)
    assert not app.sent


def test_long_text_is_split_within_limit():
    parts = ew.split_text("\n".join("x" * 80 for _ in range(40)))
    assert len(parts) > 1 and all(len(p) <= ew.MESSAGE_LIMIT for p in parts)
    assert ew.split_text("y" * 2000) == ["y" * 900, "y" * 900, "y" * 200]


def test_summary_uses_model_without_agent_or_tools(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout='{"content": "ok"}', stderr="")

    monkeypatch.setattr(ew.subprocess, "run", fake_run)
    email = {"id": "a", "from": "a@x.co", "subject": "S", "body": "B", "spam": True}
    assert ew.summarise([email]) == "ok"
    assert calls[0][:4] == ["jarvis", "ask", "--agent", ""]
    assert "--tools" not in calls[0] and "Folder: SPAM" in calls[0][-1]


def fake_install_env(tmp_path, gmail_source):
    home = tmp_path / "home"
    oj = home / ".openjarvis"
    (oj / ".venv/bin").mkdir(parents=True)
    py = oj / ".venv/bin/python"
    py.write_text("#!/bin/sh\n")
    py.chmod(0o755)
    gmail = oj / "src/src/openjarvis/connectors/gmail.py"
    gmail.parent.mkdir(parents=True)
    gmail.write_text(gmail_source)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    launchctl = bin_dir / "launchctl"
    launchctl.write_text("#!/bin/sh\nexit 0\n")
    launchctl.chmod(0o755)
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    env.pop("OPENJARVIS_HOME", None)
    return home, oj, gmail, env


def test_install_schedule_settings_and_spam_change(tmp_path):
    source = '    params = {}\n\n    resp = _gmail_get(f"{_GMAIL_API_BASE}/messages", token, params)\n'
    home, oj, gmail, env = fake_install_env(tmp_path, source)
    install = [str(REPO / "install.sh")]
    subprocess.run(install, env=env, check=True, capture_output=True)

    plist = plistlib.loads((home / "Library/LaunchAgents/com.openjarvis.email-whatsapp.plist").read_bytes())
    assert [d["Hour"] for d in plist["StartCalendarInterval"]] == [10, 13, 23]
    settings = oj / "email-whatsapp/settings.env"
    assert stat.S_IMODE(settings.stat().st_mode) == 0o600
    assert gmail.read_text().count("includeSpamTrash") == 1

    settings.write_text("CALLMEBOT_PHONE=1\nCALLMEBOT_APIKEY=k\n")
    subprocess.run(install, env=env, check=True, capture_output=True)
    assert "CALLMEBOT_APIKEY=k" in settings.read_text()
    assert gmail.read_text().count("includeSpamTrash") == 1


def test_install_warns_if_openjarvis_code_changed(tmp_path):
    _, _, gmail, env = fake_install_env(tmp_path, "something else\n")
    result = subprocess.run([str(REPO / "install.sh")], env=env, check=True, capture_output=True, text=True)
    assert "could not be turned on" in result.stderr
    assert gmail.read_text() == "something else\n"


def test_settings_file_is_not_tracked_by_git():
    ignored = (REPO / ".gitignore").read_text().splitlines()
    assert "settings.env" in ignored and "state.json" in ignored
