import imaplib, smtplib, email, os, requests
from email.message import EmailMessage
from email.header import decode_header, make_header
from email.utils import parseaddr

GMAIL = os.environ["GMAIL_ADDRESS"]
APP_PW = os.environ["GMAIL_APP_PASSWORD"]
ALLOWED = [a.strip().lower() for a in os.environ["ALLOWED_SENDERS"].split(",") if a.strip()]
API_KEY = os.environ["GEMINI_API_KEY"]
MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
TRIGGER_SUBJECT = "ai"


def ask_ai(question):
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{MODEL}:generateContent"
    r = requests.post(
        url,
        headers={"x-goog-api-key": API_KEY},
        json={"contents": [{"parts": [{"text": question}]}]},
        timeout=120,
    )
    r.raise_for_status()
    return r.json()["candidates"][0]["content"]["parts"][0]["text"]


def get_body(msg):
    parts = msg.walk() if msg.is_multipart() else [msg]
    for p in parts:
        if p.get_content_type() == "text/plain" and not p.get_filename():
            data = p.get_payload(decode=True) or b""
            text = data.decode(p.get_content_charset() or "utf-8", errors="replace")
            return "\n".join(l for l in text.splitlines() if not l.startswith(">"))
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


def main():
    imap = imaplib.IMAP4_SSL("imap.gmail.com")
    imap.login(GMAIL, APP_PW)
    imap.select("INBOX")
    for sender_addr in ALLOWED:
        _, data = imap.search(
            None, "UNSEEN", "FROM", f'"{sender_addr}"', "SUBJECT", '"AI"'
        )
        for num in data[0].split():
            # PEEK: does not mark the mail as read until we decide to
            _, msgdata = imap.fetch(num, "(BODY.PEEK[])")
            msg = email.message_from_bytes(msgdata[0][1])
            sender = parseaddr(msg["From"])[1].lower()
            subject = str(make_header(decode_header(msg.get("Subject", "")))).strip()
            if sender != sender_addr or subject.lower() != TRIGGER_SUBJECT:
                continue
            question = get_body(msg).strip()
            imap.store(num, "+FLAGS", "\\Seen")
            if not question:
                continue
            try:
                answer = ask_ai(question)
            except Exception as e:
                answer = f"שגיאה בקבלת תשובה: {type(e).__name__}"
            send_reply(sender, subject, answer, msg.get("Message-ID"))
    imap.logout()


main()
