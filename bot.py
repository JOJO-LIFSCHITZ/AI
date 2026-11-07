import imaplib, smtplib, email, os, re, time, requests
import re
from email.message import EmailMessage
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime

GMAIL = os.environ["GMAIL_ADDRESS"]
APP_PW = os.environ["GMAIL_APP_PASSWORD"]
ALLOWED = [a.strip().lower() for a in os.environ["ALLOWED_SENDERS"].split(",") if a.strip()]
API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")
TRIGGER_SUBJECT = "ai"


def normalize_subject(subject):
    s = subject.strip()
    while True:
        new_s = re.sub(r"^\s*(re|fwd|fw)\s*:\s*", "", s, flags=re.I)
        if new_s == s:
            return s.strip().lower()
        s = new_s


def ask_ai(contents, attempts=3):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    payload = {
        "contents": contents,
        # Skip extended "thinking" so the reply comes back well within the timeout.
        "generationConfig": {"thinkingConfig": {"thinkingBudget": 0}},
    }
    last_err = None
    for i in range(attempts):
        try:
            r = requests.post(
                url,
                headers={"x-goog-api-key": API_KEY},
                json=payload,
                timeout=240,
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            last_err = e
            print(f"[ask_ai] attempt {i + 1} failed: {type(e).__name__}: {e}")
            if i < attempts - 1:
                time.sleep(5)
    raise last_err


def get_body(msg):
    html_body = ""
    parts = msg.walk() if msg.is_multipart() else [msg]
    for p in parts:
        content_type = p.get_content_type()
        if not p.get_filename():
            try:
                data = p.get_payload(decode=True) or b""
                text = data.decode(p.get_content_charset() or "utf-8", errors="replace")
            except Exception:
                continue
            
            if content_type == "text/plain":
                lines = [l for l in text.splitlines() if not l.startswith(">")]
                if not lines:
                    lines = text.splitlines()
                return "\n".join(lines).strip()
            elif content_type == "text/html":
                html_body = text

    # אם נמצא רק HTML, נסיר את התגים בצורה בסיסית
    if html_body:
        clean_text = re.sub(r'<[^>]+>', '', html_body)
        return clean_text.strip()
        
    return ""


def send_reply(to, subject, body, msgid):
    m = EmailMessage()
    m["From"], m["To"] = GMAIL, to
    m["Subject"] = subject if subject.lower().startswith("re:") else "Re: " + subject
    if msgid:
        m["In-Reply-To"] = msgid
        m["References"] = msgid
    m.set_content(body)
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL, APP_PW)
        s.send_message(m)
    print(f"[send_reply] sent to {to} subject={subject!r}")


def find_all_mail_folder(imap):
    """Gmail 'All Mail' folder (works in any UI language). Falls back to INBOX."""
    _, folders = imap.list()
    for f in folders or []:
        line = f.decode(errors="replace")
        if "\\All" in line:
            return line.rsplit(' "/" ', 1)[-1]
    return "INBOX"


def get_thread_id(imap, num):
    typ, data = imap.fetch(num, "(X-GM-THRID)")
    if typ != "OK" or not data or not data[0]:
        return None
    text = data[0].decode(errors="replace") if isinstance(data[0], bytes) else str(data[0])
    match = re.search(r"X-GM-THRID\s+(\d+)", text)
    return match.group(1) if match else None


def fetch_thread_messages(imap, thrid):
    """All messages (sent + received) in this Gmail thread, oldest first."""
    typ, data = imap.search(None, "X-GM-THRID", thrid)
    if typ != "OK" or not data or not data[0]:
        return []
    out = []
    for num in data[0].split():
        _, msgdata = imap.fetch(num, "(BODY.PEEK[])")
        msg = email.message_from_bytes(msgdata[0][1])
        try:
            date = parsedate_to_datetime(msg.get("Date"))
        except Exception:
            date = None
        out.append((date, msg))
    out.sort(key=lambda t: (t[0] is None, t[0]))
    return [m for _, m in out]


def build_conversation(thread_msgs, sender_addr):
    """Turn the thread into Gemini-style alternating user/model turns."""
    contents = []
    for msg in thread_msgs:
        frm = parseaddr(msg["From"])[1].lower()
        body = get_body(msg).strip()
        if not body:
            continue
        
        raw_subj = str(make_header(decode_header(msg.get("Subject", "")))).strip()
        is_reply = raw_subj.lower().startswith("re:") or msg.get("In-Reply-To")
        
        # אם הבוט והשולח הם אותה כתובת, נבדיל לפי האם זו תשובה (Re:) או הודעה חדשה
        if GMAIL.lower() == sender_addr:
            role = "model" if is_reply else "user"
        else:
            if frm == GMAIL.lower():
                role = "model"
            elif frm == sender_addr:
                role = "user"
            else:
                continue
                
        contents.append({"role": role, "parts": [{"text": body}]})
    return contents


def main():
    print(f"[main] allowed senders: {ALLOWED}")
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL, APP_PW)
    folder = find_all_mail_folder(imap)
    print(f"[main] searching folder: {folder}")
    imap.select(folder)

    total_candidates = 0
    total_handled = 0

    for sender_addr in ALLOWED:
        _, data = imap.search(
            None, "UNSEEN", "FROM", f'"{sender_addr}"', "SUBJECT", '"AI"'
        )
        nums = data[0].split()
        print(f"[main] sender={sender_addr!r}: {len(nums)} unseen candidate(s)")
        total_candidates += len(nums)

        for num in nums:
            # PEEK: does not mark the mail as read until we decide to
            _, msgdata = imap.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(msgdata[0][1])
            sender = parseaddr(msg["From"])[1].lower()
            raw_subject = str(make_header(decode_header(msg.get("Subject", "")))).strip()
            print(f"[main]   candidate num={num}: from={sender!r} subject={raw_subject!r}")

            if sender != sender_addr or normalize_subject(raw_subject) != TRIGGER_SUBJECT:
                print("[main]   -> skipped (sender/subject mismatch)")
                continue

            imap.store(num, "+FLAGS", "\\Seen")

            # Thread-aware: replies continue the conversation, a fresh "AI" email starts a new one.
            thrid = get_thread_id(imap, num)
            if thrid:
                thread_msgs = fetch_thread_messages(imap, thrid)
                contents = build_conversation(thread_msgs, sender_addr)
            else:
                body = get_body(msg).strip()
                contents = [{"role": "user", "parts": [{"text": body}]}] if body else []

            if not contents or contents[-1]["role"] != "user":
                print("[main]   -> skipped (no usable question content)")
                continue

            try:
                answer = ask_ai(contents)
            except Exception as e:
                answer = f"שגיאה בקבלת תשובה: {type(e).__name__}"
                print(f"[main]   -> ask_ai raised: {type(e).__name__}: {e}")
            send_reply(sender, raw_subject, answer, msg.get("Message-ID"))
            total_handled += 1

    print(f"[main] done. candidates={total_candidates} handled={total_handled}")
    imap.logout()


main()
