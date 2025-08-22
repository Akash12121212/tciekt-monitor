import requests
import smtplib
import os
import time
import schedule
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import openai
from dotenv import load_dotenv
from flask import Flask
from threading import Thread

# === Load environment variables from .env ===
load_dotenv()

# === CONFIGURATION ===
FRESHDESK_DOMAIN = os.environ.get("FRESHDESK_DOMAIN")
API_KEY = os.environ.get("FRESHDESK_API_KEY")

EMAIL_FROM = os.environ.get("EMAIL_FROM")
EMAIL_PASS = os.environ.get("EMAIL_PASS")
EMAIL_TO = os.environ.get("EMAIL_TO")
TEAMS_CHANNEL_EMAIL = os.environ.get("TEAMS_CHANNEL_EMAIL")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
PROCESSED_TICKETS_FILE = "processed_tickets.txt"

# === OpenAI Client ===
client = openai.OpenAI(api_key=OPENAI_API_KEY)

# === Flask Web Server ===
app = Flask("")


@app.route("/")
def home():
    return "✅ Ticket monitor is running."


# === LOGGING ===
def log_event(message):
    with open("ticket_log.txt", "a") as log_file:
        log_file.write(f"[{datetime.now(timezone.utc).isoformat()}] {message}\n")
    print(message, flush=True)  # Also print to Render logs


# === FETCH TICKETS ===
def fetch_recent_tickets():
    url = f"https://{FRESHDESK_DOMAIN}/api/v2/tickets?order_type=desc&page=1&per_page=100"
    try:
        response = requests.get(url, auth=(API_KEY, "X"))
        log_event(f"🔍 API Status: {response.status_code}")
        if response.status_code == 200:
            return response.json()
        else:
            log_event(f"❌ Error fetching tickets: {response.text}")
            return []
    except Exception as e:
        log_event(f"❌ Request failed: {e}")
        return []


# === GPT URGENCY DETECTION ===
def is_urgent(text):
    prompt = (
        "You are an assistant that classifies whether a customer support ticket is urgent.\n"
        "Reply with only `true` if the message is urgent (e.g. broken, down, critical, high priority), "
        "or `false` otherwise. Understand the tone of the customer to identify whether it is urgent or not.\n\n"
        f"Ticket message:\n{text}\n\nIs this urgent?")
    try:
        response = client.chat.completions.create(
            model="gpt-4.1-nano",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            max_tokens=5,
        )
        result = response.choices[0].message.content.strip().lower()
        log_event(f"🧠 GPT-4.1 Response: {result}")
        return "true" in result
    except Exception as e:
        log_event(f"❌ GPT API error: {e}")
        return False


# === SEND EMAIL ALERT ===
def send_alert_email(subject, body, ticket_url):
    msg = MIMEMultipart()
    msg["From"] = EMAIL_FROM
    msg["To"] = EMAIL_TO
    msg["Subject"] = f"🚨 P0 Ticket Alert: {subject}"

    recipients = [EMAIL_TO, TEAMS_CHANNEL_EMAIL]
    html_content = f"""
    <html>
      <body>
        <p>{body}</p>
        <p>🔗 <a href="{ticket_url}">View Ticket</a></p>
      </body>
    </html>
    """
    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP("smtp.office365.com", 587) as server:
            server.starttls()
            server.login(EMAIL_FROM, EMAIL_PASS)
            log_event(f"📨 Sending email to: {recipients}")
            server.sendmail(EMAIL_FROM, recipients, msg.as_string())
            log_event("✅ Email sent!")
    except Exception as e:
        log_event(f"❌ Failed to send email: {e}")


# === TRACK PROCESSED TICKETS ===
def read_processed_ids():
    if os.path.exists(PROCESSED_TICKETS_FILE):
        with open(PROCESSED_TICKETS_FILE, "r") as f:
            return set(line.strip() for line in f.readlines())
    return set()


def mark_processed(ticket_id):
    with open(PROCESSED_TICKETS_FILE, "a") as f:
        f.write(f"{ticket_id}\n")


# === MAIN LOGIC ===
def check_recent_tickets():
    log_event("\n🔄 Checking tickets...")
    tickets = fetch_recent_tickets()
    log_event(f"Found {len(tickets)} tickets from API")
    if not tickets:
        return

    processed_ids = read_processed_ids()
    recent_cutoff = datetime.now(timezone.utc) - timedelta(minutes=1)

    for ticket in tickets:
        ticket_id = str(ticket["id"])
        created_at = datetime.fromisoformat(ticket["created_at"].replace("Z", "+00:00"))

        if ticket_id in processed_ids:
            continue
        if created_at < recent_cutoff:
            continue
        if ticket.get("responder_id") is not None:
            continue

        subject = ticket.get("subject", "")
        description = ticket.get("description", "")
        full_text = f"{subject} {description}".strip()
        ticket_url = f"https://{FRESHDESK_DOMAIN}/a/tickets/{ticket_id}"

        mark_processed(ticket_id)

        try:
            urgent = is_urgent(full_text)
            log_event(f"Processed ticket {ticket_id} | urgent={urgent}")
            if urgent:
                log_event(f"🚨 Urgent ticket detected: {ticket_id}")
                send_alert_email(subject or "No Subject", description or "No Description", ticket_url)
            else:
                log_event(f"✅ Ticket {ticket_id} is not urgent.")
        except Exception as e:
            log_event(f"❌ Error processing ticket {ticket_id}: {e}")


# === SCHEDULE JOB ===
def schedule_job():
    schedule.every(1).minutes.do(check_recent_tickets)
    log_event("⏱️ Scheduled to run every 1 minute.")
    while True:
        schedule.run_pending()
        time.sleep(10)


# === ENTRY POINT ===
if __name__ == "__main__":
    # Run scheduler in background
    Thread(target=schedule_job).start()

    # Run one immediate check for testing
    check_recent_tickets()

    # Run Flask in main thread (Render expects this)
    app.run(host="0.0.0.0", port=8080)
