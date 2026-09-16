import configparser
import email
import imaplib
import os
import smtplib
from email.header import decode_header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime

from fastmcp import FastMCP

mcp = FastMCP("email_mcp")

config = configparser.ConfigParser()
config_path = os.path.expanduser(
    os.getenv("EMAIL_MCP_CONFIG_PATH") or os.path.join(os.path.dirname(__file__), "config.ini")
)
config.read(config_path, encoding="utf-8")


_ENV_OVERRIDES = {
    ("ACCOUNT", "email"): "EMAIL_MCP_ADDRESS",
    ("ACCOUNT", "password"): "EMAIL_MCP_PASSWORD",
}


def get_config(section: str, key: str) -> str:
    env_name = _ENV_OVERRIDES.get((section, key))
    if env_name:
        value = os.getenv(env_name)
        if value is not None and value.strip():
            return value.strip()

    if not config.has_option(section, key):
        raise RuntimeError(
            f"Missing email MCP setting [{section}] {key!r}; "
            f"copy config.example.ini to config.ini or set {env_name or 'the corresponding environment variable'}"
        )

    value = config.get(section, key).strip()
    if not value:
        raise RuntimeError(f"Email MCP setting [{section}] {key!r} is empty")
    return value


def decode_str(s):
    if s is None:
        return ""

    value, charset = decode_header(s)[0]
    if charset:
        try:
            value = value.decode(charset)
        except Exception:
            try:
                value = value.decode("utf-8")
            except Exception:
                value = value.decode("gbk", errors="ignore")
    elif isinstance(value, bytes):
        try:
            value = value.decode("utf-8")
        except Exception:
            try:
                value = value.decode("gbk", errors="ignore")
            except Exception:
                value = str(value)
    return str(value)


def get_email_body(msg):
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))

            if "attachment" in content_disposition:
                continue

            if content_type == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="ignore")
                    break
                except Exception:
                    continue
            elif content_type == "text/html" and not body:
                try:
                    payload = part.get_payload(decode=True)
                    charset = part.get_content_charset() or "utf-8"
                    body = payload.decode(charset, errors="ignore")
                except Exception:
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            charset = msg.get_content_charset() or "utf-8"
            body = payload.decode(charset, errors="ignore")
        except Exception:
            body = str(msg.get_payload())

    return body.strip()


@mcp.tool()
def fetch_unread_emails(max_count: int = 10) -> str:
    try:
        imap_server = get_config("IMAP", "server")
        imap_port = int(get_config("IMAP", "port"))
        email_account = get_config("ACCOUNT", "email")
        email_password = get_config("ACCOUNT", "password")

        mail = imaplib.IMAP4_SSL(imap_server, imap_port)
        mail.login(email_account, email_password)

        try:
            imap_id = ("name", "EmailMCP", "version", "1.0", "vendor", "FastMCP")
            typ, data = mail.xatom("ID", '("' + '" "'.join(imap_id) + '")')
            print(f"ID command result - typ: {typ}, data: {data}")
        except Exception as e:
            print(f"ID command failed (may not be required): {e}")

        result, message = mail.select("INBOX")
        if result != "OK":
            return f"Failed to select INBOX: {message}"
        print(f"Selected INBOX - result: {result}, message: {message}")

        status, messages = mail.search(None, "UNSEEN")
        if status != "OK":
            return "Failed to fetch unread emails."

        email_ids = messages[0].split()

        if not email_ids:
            mail.logout()
            return "No unread emails."

        email_ids = email_ids[-max_count:]

        unread_emails = []

        for email_id in email_ids:
            status, msg_data = mail.fetch(email_id, "(RFC822)")
            if status != "OK":
                continue

            raw_email = msg_data[0][1]
            msg = email.message_from_bytes(raw_email)

            subject = decode_str(msg.get("Subject", ""))
            from_addr = decode_str(msg.get("From", ""))
            date_str = msg.get("Date", "")

            try:
                date_obj = parsedate_to_datetime(date_str)
                formatted_date = date_obj.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                formatted_date = date_str

            body = get_email_body(msg)

            if len(body) > 1000:
                body = body[:1000] + "...(content too long, truncated)"

            email_info = {
                "id": email_id.decode(),
                "subject": subject,
                "from": from_addr,
                "date": formatted_date,
                "body": body,
            }

            unread_emails.append(email_info)

        mail.logout()

        result_text = f"Total unread emails: {len(unread_emails)}\n\n"
        for idx, email_info in enumerate(unread_emails, 1):
            result_text += f"[Email {idx}]\n"
            result_text += f"Subject: {email_info['subject']}\n"
            result_text += f"From: {email_info['from']}\n"
            result_text += f"Date: {email_info['date']}\n"
            result_text += f"Body:\n{email_info['body']}\n"
            result_text += "-" * 50 + "\n\n"

        return result_text

    except Exception as e:
        return f"Error while fetching unread emails: {str(e)}"


@mcp.tool()
def send_email(to_addr: str, subject: str, body: str, cc: str = "") -> str:
    try:
        smtp_server = get_config("SMTP", "server")
        smtp_port = int(get_config("SMTP", "port"))
        email_account = get_config("ACCOUNT", "email")
        email_password = get_config("ACCOUNT", "password")

        msg = MIMEMultipart()
        msg["From"] = email_account
        msg["To"] = to_addr
        msg["Subject"] = subject

        if cc:
            msg["Cc"] = cc

        msg.attach(MIMEText(body, "plain", "utf-8"))

        server = smtplib.SMTP_SSL(smtp_server, smtp_port)
        server.login(email_account, email_password)

        recipients = [to_addr]
        if cc:
            recipients.extend([addr.strip() for addr in cc.split(",")])

        server.sendmail(email_account, recipients, msg.as_string())
        server.quit()

        return f"Email sent successfully!\nTo: {to_addr}\nSubject: {subject}"

    except Exception as e:
        return f"Error while sending email: {str(e)}"


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Email MCP Server (FastMCP)")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default=os.getenv("EMAIL_MCP_TRANSPORT", "stdio"),
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--host", default=os.getenv("EMAIL_MCP_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(os.getenv("EMAIL_MCP_PORT", "3456")))
    args = parser.parse_args()

    if args.transport == "sse":
        mcp.run(transport="sse", port=args.port, host=args.host)
    else:
        mcp.run(transport="stdio")
