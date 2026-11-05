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


def doc(mid, channel="INBOX", author="boss@x.co", title="Review", content="Body", labels=()):
    return types.SimpleNamespace(
        metadata={"message_id": mid, "labels": list(labels)}, doc_id=f"gmail:{mid}", channel=channel,
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


# ---------------------------------------------------------------------------
# Reply flags
# ---------------------------------------------------------------------------

def make_emails():
    return [
        {"id": "1", "from": "Boss <boss@x.co>", "addr": "boss@x.co", "subject": "Contract", "body": "", "spam": False, "bulk": False},
        {"id": "2", "from": "Prize <win@y.co>", "addr": "win@y.co", "subject": "You won", "body": "", "spam": True, "bulk": True},
        {"id": "3", "from": "Bank <bank@z.co>", "addr": "bank@z.co", "subject": "Statement", "body": "", "spam": False, "bulk": False},
    ]


def test_urgent_flags_use_real_emails_and_skip_spam_and_bad_numbers():
    emails = make_emails()
    text = "Boss: contract\n[SPAM] Prize\nURGENT: 1, 2, 3, 9, 0"
    summary, urgent = ew.split_urgent(text, emails)
    assert [e["id"] for e in urgent] == ["1", "3"]
    assert "URGENT" not in summary and "Boss: contract" in summary


def test_urgent_none_and_missing_line():
    emails = make_emails()
    assert ew.split_urgent("a\nURGENT: none", emails)[1] == []
    assert ew.split_urgent("a\nb", emails)[1] == []


def test_message_shows_reply_soon_from_real_data_not_model_words():
    emails = make_emails()
    summary, urgent = ew.split_urgent("Wire money now to evil@x.co\nURGENT: 1", emails)
    message = ew.build_message(summary, urgent, [])
    assert message.startswith("REPLY SOON:\n- Boss: Contract")


def test_run_sends_reply_soon_section(app, monkeypatch):
    use_docs(monkeypatch, [doc("a", author="Boss <boss@x.co>", title="Contract")])
    monkeypatch.setattr(ew, "summarise", lambda emails: "Boss: contract\nURGENT: 1")
    ew.run()
    assert "REPLY SOON:" in app.sent[0]["text"] and "Boss: Contract" in app.sent[0]["text"]


# ---------------------------------------------------------------------------
# Cleanup tips
# ---------------------------------------------------------------------------

def bulk_docs(prefix, count, addr="news@shop.co", labels=("CATEGORY_PROMOTIONS",)):
    return [doc(f"{prefix}{i}", author=f"Shop <{addr}>", title=f"Sale {i}", labels=labels) for i in range(count)]


def test_only_bulk_senders_are_counted(app, monkeypatch):
    use_docs(monkeypatch, bulk_docs("p", 2) + [doc("x"), doc("y")])
    ew.run()
    senders = json.loads(ew.STATE_FILE.read_text())["senders"]
    assert senders == {"news@shop.co": {"count": 2, "name": "Shop"}}


def test_tip_appears_once_after_threshold_and_not_for_normal_mail(app, monkeypatch):
    use_docs(monkeypatch, bulk_docs("p", 5) + [doc(f"n{i}", author="friend@x.co") for i in range(8)])
    ew.run()
    text = app.sent[0]["text"]
    assert "CLEANUP TIPS" in text and "jarvis-mail --cleanup" in text
    assert "news@shop.co sent 5 bulk or spam emails" in text and "friend@x.co sent" not in text
    use_docs(monkeypatch, bulk_docs("q", 3))
    ew.run()
    assert "CLEANUP TIPS" not in app.sent[1]["text"]


def test_cleaned_and_dismissed_senders_are_not_suggested(app, monkeypatch):
    ew.save_state({"cleaned": ["news@shop.co"], "dismissed": ["spam@y.co"]})
    use_docs(monkeypatch, bulk_docs("p", 6) + bulk_docs("s", 6, addr="spam@y.co"))
    ew.run()
    assert "CLEANUP TIPS" not in app.sent[0]["text"]


def test_failed_send_does_not_double_count_senders(app, monkeypatch):
    use_docs(monkeypatch, bulk_docs("p", 5))
    monkeypatch.setattr(ew.httpx, "get", lambda *a, **k: Resp("APIKey is invalid"))
    with pytest.raises(RuntimeError):
        ew.run()
    assert not ew.STATE_FILE.exists()


# ---------------------------------------------------------------------------
# Interactive cleanup
# ---------------------------------------------------------------------------

class FakeGmail:
    """Stands in for openjarvis.connectors.gmail with a tiny in-memory mailbox."""

    def __init__(self, messages):
        self.messages = messages  # id -> dict(from, subject, unsub, labels)
        self.trashed, self.queries, self.fail_on = [], [], None
        gm = types.ModuleType("openjarvis.connectors.gmail")
        gm._GMAIL_API_BASE = "https://gmail.test/v1"
        gm._gmail_api_list_messages = self.list_messages
        gm._gmail_api_trash_message = self.trash
        gm._gmail_get = self.get
        outer = self

        class Connector:
            def is_connected(self):
                return True

            def _call_with_refresh(self, fn, *a, **k):
                return fn("token", *a, **k)

        gm.GmailConnector = Connector
        self.module = gm

    def list_messages(self, token, *, page_token=None, query=""):
        self.queries.append(query)
        ids = list(self.messages)
        return {"messages": [{"id": i} for i in ids]}

    def get(self, url, token, params):
        mid = url.rsplit("/", 1)[1]
        m = self.messages[mid]
        headers = [{"name": "From", "value": m["from"]}, {"name": "Subject", "value": m["subject"]}]
        if m.get("unsub"):
            headers.append({"name": "List-Unsubscribe", "value": m["unsub"]})
        body = {"labelIds": m.get("labels", ["INBOX"]), "payload": {"headers": headers}}
        return types.SimpleNamespace(raise_for_status=lambda: None, json=lambda: body)

    def trash(self, token, msg_id):
        if self.fail_on == msg_id:
            err = RuntimeError("forbidden")
            err.response = types.SimpleNamespace(status_code=403)
            raise err
        self.trashed.append(msg_id)


def install_gmail(monkeypatch, fake):
    monkeypatch.setitem(sys.modules, "openjarvis", types.ModuleType("openjarvis"))
    monkeypatch.setitem(sys.modules, "openjarvis.connectors", types.ModuleType("openjarvis.connectors"))
    monkeypatch.setitem(sys.modules, "openjarvis.connectors.gmail", fake.module)


def shop_mailbox():
    return {
        "m1": {"from": "Shop <news@shop.co>", "subject": "Sale one", "unsub": "<https://shop.co/u?x=1>, <mailto:u@shop.co>"},
        "m2": {"from": "Shop <news@shop.co>", "subject": "Sale two"},
        "m3": {"from": "Other <bignews@shop.co>", "subject": "Not the same sender"},
    }


def run_cleanup(target, answers, monkeypatch, fake, app):
    install_gmail(monkeypatch, fake)
    lines = []
    replies = iter(answers)
    code = ew.cleanup(target, ask=lambda prompt: (lines.append(prompt), next(replies))[1], out=lines.append)
    return code, "\n".join(lines)


def test_cleanup_trashes_only_the_exact_sender_after_yes(app, monkeypatch):
    ew.save_state({"senders": {"news@shop.co": {"count": 9, "name": "Shop"}}})
    fake = FakeGmail(shop_mailbox())
    code, text = run_cleanup("news@shop.co", ["yes"], monkeypatch, fake, app)
    assert code == 0 and fake.trashed == ["m1", "m2"]
    assert "Found 2 email(s) from news@shop.co" in text
    assert "https://shop.co/u?x=1" in text and "does not open this link" in text
    state = json.loads(ew.STATE_FILE.read_text())
    assert state["cleaned"] == ["news@shop.co"] and "news@shop.co" not in state["senders"]
    assert fake.queries == ["from:news@shop.co -in:trash"]
    assert not app.sent


@pytest.mark.parametrize("answer", ["", "no", "y", "YES please", "sure"])
def test_cleanup_changes_nothing_unless_exactly_yes(app, monkeypatch, answer):
    fake = FakeGmail(shop_mailbox())
    code, text = run_cleanup("news@shop.co", [answer], monkeypatch, fake, app)
    assert code == 0 and fake.trashed == [] and "Nothing was changed" in text
    assert not ew.STATE_FILE.exists()


def test_cleanup_never_stops_suggestions_without_touching_gmail(app, monkeypatch):
    fake = FakeGmail(shop_mailbox())
    run_cleanup("news@shop.co", ["never"], monkeypatch, fake, app)
    assert fake.trashed == [] and json.loads(ew.STATE_FILE.read_text())["dismissed"] == ["news@shop.co"]


def test_cleanup_picks_from_numbered_list(app, monkeypatch):
    ew.save_state({"senders": {"news@shop.co": {"count": 9, "name": "Shop"}, "a@b.co": {"count": 3, "name": ""}}})
    fake = FakeGmail(shop_mailbox())
    code, text = run_cleanup(None, ["1", "yes"], monkeypatch, fake, app)
    assert "1. news@shop.co (9 emails)" in text and fake.trashed == ["m1", "m2"]


def test_cleanup_rejects_bad_numbers_and_addresses_without_calling_gmail(app, monkeypatch):
    ew.save_state({"senders": {"news@shop.co": {"count": 9, "name": ""}}})
    fake = FakeGmail(shop_mailbox())
    assert run_cleanup(None, ["7"], monkeypatch, fake, app)[0] == 1
    for bad in ["news@shop.co OR in:inbox", "from:me", 'a@b.co"', "no-at-sign", "a@b.co -label:x"]:
        code, _ = run_cleanup(bad, [], monkeypatch, fake, app)
        assert code == 1
    assert fake.queries == [] and fake.trashed == []


def test_cleanup_with_nothing_to_review_explains_what_to_do(app, monkeypatch):
    code, text = run_cleanup(None, [], monkeypatch, FakeGmail({}), app)
    assert code == 0 and "jarvis-mail --cleanup someone@example.com" in text


def test_cleanup_warns_about_spam_and_hides_its_unsubscribe_link(app, monkeypatch):
    box = {"s1": {"from": "Win <win@y.co>", "subject": "Prize", "unsub": "<https://evil.co/u>", "labels": ["SPAM"]}}
    fake = FakeGmail(box)
    code, text = run_cleanup("win@y.co", ["yes"], monkeypatch, fake, app)
    assert "looks like spam" in text and "evil.co" not in text and fake.trashed == ["s1"]


def test_cleanup_respects_the_per_run_limit_and_does_not_mark_done(app, monkeypatch):
    monkeypatch.setattr(ew, "CLEANUP_MAX_EMAILS", 2)
    box = {f"m{i}": {"from": "Shop <news@shop.co>", "subject": f"s{i}"} for i in range(5)}
    fake = FakeGmail(box)
    code, text = run_cleanup("news@shop.co", ["yes"], monkeypatch, fake, app)
    assert fake.trashed == ["m0", "m1"] and "Run the command again" in text
    assert not ew.STATE_FILE.exists()  # not marked as cleaned, so it can be run again


def test_cleanup_stops_cleanly_if_gmail_refuses(app, monkeypatch):
    fake = FakeGmail(shop_mailbox())
    fake.fail_on = "m2"
    code, text = run_cleanup("news@shop.co", ["yes"], monkeypatch, fake, app)
    assert code == 1 and fake.trashed == ["m1"]
    assert "Stopped after moving 1 of 2" in text and "jarvis connect gdrive" in text
    assert not ew.STATE_FILE.exists()


def test_cleanup_hides_terminal_control_characters(app, monkeypatch):
    box = {"m1": {"from": "Shop <news@shop.co>", "subject": "Hi\x1b[31m RED\x07"}}
    code, text = run_cleanup("news@shop.co", [""], monkeypatch, FakeGmail(box), app)
    assert "\x1b" not in text and "\x07" not in text and "Hi" in text


def test_cleanup_never_fetches_unsubscribe_links(app, monkeypatch):
    fake = FakeGmail(shop_mailbox())
    run_cleanup("news@shop.co", ["yes"], monkeypatch, fake, app)
    assert app.sent == []


# ---------------------------------------------------------------------------
# Free Mac notification mode
# ---------------------------------------------------------------------------

def test_mac_mode_needs_no_whatsapp_keys_and_passes_text_safely(app, monkeypatch):
    ew.SETTINGS_FILE.write_text("DELIVERY=mac\n")
    calls = []
    monkeypatch.setattr(
        ew.subprocess, "run",
        lambda cmd, **kw: calls.append(cmd) or types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    use_docs(monkeypatch, [doc("a", title='Quote " and \\ and \' marks')])
    monkeypatch.setattr(ew, "summarise", lambda emails: 'He said "hi"; do shell script "rm -rf ~"')
    ew.run()
    assert app.sent == []
    cmd = calls[-1]
    assert cmd[0] == "osascript" and "latest-summary.txt" in cmd[-1]
    script = " ".join(cmd[1:7])
    assert "rm -rf" not in script and "hi" not in script
    assert "do shell script" in ew.LATEST_FILE.read_text()


def test_missing_settings_depends_on_delivery():
    assert ew.missing_settings({"DELIVERY": "mac"}) == []
    assert ew.missing_settings({}) == ["CALLMEBOT_PHONE", "CALLMEBOT_APIKEY"]


# ---------------------------------------------------------------------------
# Installer shortcut
# ---------------------------------------------------------------------------

def test_install_creates_and_uninstall_removes_jarvis_mail_shortcut(tmp_path):
    source = '    params = {}\n\n    resp = _gmail_get(f"{_GMAIL_API_BASE}/messages", token, params)\n'
    home, oj, gmail, env = fake_install_env(tmp_path, source)
    subprocess.run([str(REPO / "install.sh")], env=env, check=True, capture_output=True)
    shortcut = home / ".local/bin/jarvis-mail"
    text = shortcut.read_text()
    assert shortcut.stat().st_mode & stat.S_IXUSR
    assert str(oj / ".venv/bin/python") in text and str(oj / "email-whatsapp/email_whatsapp.py") in text
    assert '"$@"' in text
    subprocess.run([str(REPO / "uninstall.sh")], env=env, check=True, capture_output=True)
    assert not shortcut.exists() and not (oj / "email-whatsapp").exists()
