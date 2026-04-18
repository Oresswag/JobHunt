#!/usr/bin/env python3
"""Autonomous job email processor with SQLite tracking and PDF artifact output."""

import email
import imaplib
import json
import logging
import os
import re
import sqlite3
import sys
import time
from contextlib import suppress
from datetime import datetime
from email.message import Message
from functools import lru_cache
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import markdown
import pdfkit
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


# --- CONFIGURATION ---
IMAP_SERVER = env("IMAP_SERVER", "imap.gmail.com")
EMAIL_ACCOUNT = env("EMAIL_ACCOUNT", "your_email@gmail.com")
APP_PASSWORD = env("APP_PASSWORD", "your_16_digit_app_password")
DISCORD_WEBHOOK_URL = env("DISCORD_WEBHOOK_URL", "your_discord_webhook_url_here")
LLM_API_URL = env("LLM_API_URL", "http://127.0.0.1:11434/api/generate")
MODEL_NAME = env("MODEL_NAME", "qwen2.5:7b-instruct")
INBOX_FOLDER = env("INBOX_FOLDER", "inbox")
EMAIL_FROM_FILTER = env("EMAIL_FROM_FILTER", "alert@indeed.com")
WKHTMLTOPDF_PATH = env("WKHTMLTOPDF_PATH", "")
MATCH_THRESHOLD = int(env("MATCH_THRESHOLD", "75"))
PLAYWRIGHT_NAV_TIMEOUT_MS = int(env("PLAYWRIGHT_NAV_TIMEOUT_MS", "20000"))
PLAYWRIGHT_TEXT_TIMEOUT_MS = int(env("PLAYWRIGHT_TEXT_TIMEOUT_MS", "8000"))
LLM_REQUEST_TIMEOUT_SECONDS = int(env("LLM_REQUEST_TIMEOUT_SECONDS", "60"))
JOB_DELAY_SECONDS = float(env("JOB_DELAY_SECONDS", "1.5"))


# --- PATHS ---
def resolve_path(name: str, default: Path) -> Path:
    value = Path(env(name, str(default))).expanduser()
    return value if value.is_absolute() else (BASE_DIR / value).resolve()


DB_PATH = resolve_path("DATABASE_PATH", BASE_DIR / "jobs.db")
QUEUE_DIR = resolve_path("QUEUE_DIR", BASE_DIR / "queue")
CHROME_PROFILE_DIR = resolve_path("CHROME_PROFILE_DIR", BASE_DIR / "chrome-profile")
LOG_PATH = resolve_path("LOG_PATH", BASE_DIR / "job_agent.log")
CANDIDATE_PROFILE_PATH = resolve_path("CANDIDATE_PROFILE_PATH", BASE_DIR / "candidate_profile.md")

QUEUE_DIR.mkdir(parents=True, exist_ok=True)
CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("job_agent")
logger.setLevel(logging.INFO)
logger.handlers.clear()
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

file_handler = RotatingFileHandler(
    LOG_PATH,
    maxBytes=1_000_000,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setFormatter(formatter)

stream_handler = logging.StreamHandler(sys.stdout)
stream_handler.setFormatter(formatter)

logger.addHandler(file_handler)
logger.addHandler(stream_handler)
logger.propagate = False


@lru_cache(maxsize=1)
def candidate_profile() -> str:
    if not CANDIDATE_PROFILE_PATH.exists():
        raise RuntimeError(
            f"Candidate profile file was not found at {CANDIDATE_PROFILE_PATH}. "
            "Create candidate_profile.md or point CANDIDATE_PROFILE_PATH at the right file."
        )

    content = CANDIDATE_PROFILE_PATH.read_text(encoding="utf-8").strip()
    if not content:
        raise RuntimeError(f"Candidate profile file is empty: {CANDIDATE_PROFILE_PATH}")
    return content


def validate_config() -> list[str]:
    missing = []
    required_values = {
        "EMAIL_ACCOUNT": EMAIL_ACCOUNT,
        "APP_PASSWORD": APP_PASSWORD,
        "DISCORD_WEBHOOK_URL": DISCORD_WEBHOOK_URL,
        "LLM_API_URL": LLM_API_URL,
        "MODEL_NAME": MODEL_NAME,
    }

    placeholder_markers = {
        "your_email@gmail.com",
        "your_16_digit_app_password",
        "your_discord_webhook_url_here",
    }

    for key, value in required_values.items():
        if not value or value in placeholder_markers:
            missing.append(key)

    if not CANDIDATE_PROFILE_PATH.exists():
        missing.append("CANDIDATE_PROFILE_PATH")

    return missing


def retry(
    operation_name: str,
    func: Callable[[], Any],
    *,
    attempts: int = 3,
    base_delay: float = 2.0,
    retryable: Sequence[type[BaseException]] = (Exception,),
) -> Any:
    """Retry a callable with exponential backoff for expected errors only."""
    last_error: Optional[BaseException] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except tuple(retryable) as exc:  # type: ignore[arg-type]
            last_error = exc
            if attempt == attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "%s failed on attempt %s/%s: %s. Retrying in %.1fs.",
                operation_name,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)

    if last_error is None:
        raise RuntimeError(f"{operation_name} failed without raising a retryable exception")
    raise last_error


def setup_database() -> None:
    """Initialize the SQLite database for tracking processed jobs."""
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                url TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                score INTEGER NOT NULL,
                status TEXT NOT NULL,
                timestamp TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_status_timestamp ON jobs(status, timestamp)"
        )


def job_exists(url: str) -> bool:
    """Return True if the job URL has already been processed."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.execute("SELECT 1 FROM jobs WHERE url = ? LIMIT 1", (url,))
        return cursor.fetchone() is not None


def log_job(url: str, title: str, score: int, status: str) -> None:
    """Insert or update a job row in SQLite."""
    timestamp = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO jobs (url, title, score, status, timestamp)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                title = excluded.title,
                score = excluded.score,
                status = excluded.status,
                timestamp = excluded.timestamp
            """,
            (url, title, score, status, timestamp),
        )


def send_discord_notification(message: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        return

    def perform_request() -> None:
        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"content": message},
            timeout=15,
        )
        response.raise_for_status()

    try:
        retry(
            "Discord webhook",
            perform_request,
            attempts=3,
            base_delay=2,
            retryable=(requests.RequestException,),
        )
    except requests.RequestException as exc:
        logger.error("Failed to send Discord alert: %s", exc)


def normalize_text(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"\r\n?", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


def extract_job_content(page) -> Optional[str]:
    """Prefer the Indeed job description container, then fall back to visible text."""
    selectors = [
        "#jobDescriptionText",
        '[data-testid="jobsearch-JobComponent-description"]',
    ]
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            locator.wait_for(state="visible", timeout=PLAYWRIGHT_TEXT_TIMEOUT_MS)
            text = normalize_text(locator.inner_text(timeout=PLAYWRIGHT_TEXT_TIMEOUT_MS))
            if text:
                return text
        except PlaywrightTimeoutError:
            continue
        except PlaywrightError:
            continue

    try:
        fallback_text = page.locator("body").inner_text(timeout=PLAYWRIGHT_TEXT_TIMEOUT_MS)
    except PlaywrightError:
        fallback_text = page.evaluate("document.body ? document.body.innerText : ''")

    return normalize_text(fallback_text) if fallback_text else None


def extract_job_playwright(url: str) -> Optional[str]:
    """Use Playwright with a persistent profile to extract JS-rendered text."""
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch_persistent_context(
                user_data_dir=str(CHROME_PROFILE_DIR),
                headless=True,
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            )
            try:
                page = browser.pages[0] if browser.pages else browser.new_page()
                retry(
                    f"Playwright navigation for {url}",
                    lambda: page.goto(url, wait_until="domcontentloaded", timeout=PLAYWRIGHT_NAV_TIMEOUT_MS),
                    attempts=2,
                    base_delay=1,
                    retryable=(PlaywrightTimeoutError, PlaywrightError),
                )
                with suppress(PlaywrightError):
                    page.wait_for_load_state("networkidle", timeout=5_000)
                return extract_job_content(page)
            finally:
                browser.close()
    except PlaywrightTimeoutError as exc:
        logger.warning("Playwright timed out for %s: %s", url, exc)
        return None
    except PlaywrightError as exc:
        logger.error("Playwright extraction failed for %s: %s", url, exc)
        return None


def pdfkit_config() -> Optional[pdfkit.configuration]:
    if not WKHTMLTOPDF_PATH:
        return None
    return pdfkit.configuration(wkhtmltopdf=WKHTMLTOPDF_PATH)


def call_llm(prompt: str, response_format: Optional[str] = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": MODEL_NAME,
        "prompt": prompt,
        "stream": False,
    }
    if response_format:
        payload["format"] = response_format

    def perform_request() -> dict[str, Any]:
        response = requests.post(LLM_API_URL, json=payload, timeout=LLM_REQUEST_TIMEOUT_SECONDS)
        response.raise_for_status()
        return response.json()

    return retry(
        "LLM request",
        perform_request,
        attempts=2,
        base_delay=2,
        retryable=(requests.RequestException,),
    )


def extract_json_object(raw_response: str) -> dict[str, Any]:
    raw = (raw_response or "").strip()
    if not raw:
        raise ValueError("LLM returned empty output")

    cleaned = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)

    candidates = [cleaned]
    match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if match:
        candidates.append(match.group(0))

    seen: set[str] = set()
    errors: list[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(str(exc))
            continue
        if not isinstance(parsed, dict):
            errors.append("JSON root was not an object")
            continue
        return parsed

    raise ValueError(
        "Could not extract valid JSON from LLM output. "
        f"Sample: {cleaned[:300]!r}. Errors: {errors}"
    )


def parse_evaluation_response(response_json: dict[str, Any]) -> tuple[int, str]:
    result = extract_json_object(str(response_json.get("response", "")))

    raw_score = result.get("match_score")
    if raw_score is None:
        raise ValueError("LLM response was missing match_score")
    score = max(0, min(int(raw_score), 100))

    title = normalize_text(str(result.get("job_title", "Unknown Job"))) or "Unknown Job"
    return score, title


def sanitize_markdown_output(raw_markdown: str) -> str:
    cleaned = (raw_markdown or "").strip()
    cleaned = re.sub(r"^```(?:markdown|md)?\s*", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return cleaned.strip()


def safe_stem(text: str, max_length: int = 60) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")
    return cleaned[:max_length] or "job"


def write_text_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def generate_artifacts(job_text: str, url: str, title: str) -> dict[str, Optional[Path]]:
    """Generate Markdown, save raw artifacts, and convert the resume into a PDF."""
    prompt = f"""
You are an expert resume writer. Rewrite the candidate's resume to highly emphasize the skills required in the job description.
Output ONLY valid Markdown. Do not include any conversational text.

Candidate Resume: {candidate_profile()}
Job Description: {job_text[:3000]}
Source URL: {url}
""".strip()

    timestamp = int(time.time())
    stem = f"{timestamp}_{safe_stem(title)}"
    job_text_path = write_text_file(QUEUE_DIR / f"{stem}_job.txt", job_text)

    try:
        response_json = call_llm(prompt)
        md_content = sanitize_markdown_output(str(response_json.get("response", "")))
        if not md_content:
            raise ValueError("LLM returned an empty Markdown resume.")
        markdown_path = write_text_file(QUEUE_DIR / f"{stem}_resume.md", md_content)

        html_content = markdown.markdown(md_content)
        styled_html = f"""
        <html>
          <head>
            <meta charset="utf-8" />
            <style>
              body {{ font-family: Arial, sans-serif; line-height: 1.6; margin: 40px; }}
              h1, h2, h3 {{ color: #333; }}
            </style>
          </head>
          <body>{html_content}</body>
        </html>
        """

        file_path = QUEUE_DIR / f"{stem}_resume.pdf"
        pdfkit.from_string(styled_html, str(file_path), configuration=pdfkit_config())
        return {
            "job_text_path": job_text_path,
            "markdown_path": markdown_path,
            "pdf_path": file_path,
        }
    except (requests.RequestException, ValueError, OSError, IOError) as exc:
        logger.exception("Artifact generation failed for %s: %s", url, exc)
        return {
            "job_text_path": job_text_path,
            "markdown_path": None,
            "pdf_path": None,
        }


def evaluate_and_process(job_text: str, url: str) -> None:
    prompt = f"""
Review the job description against the candidate's resume.
Output strictly in JSON with keys "match_score" (0-100 integer) and "job_title" (string).
Candidate: {candidate_profile()}
Job: {job_text[:3000]}
Source URL: {url}
""".strip()

    try:
        response_json = call_llm(prompt, response_format="json")
        score, title = parse_evaluation_response(response_json)

        if score >= MATCH_THRESHOLD:
            artifacts = generate_artifacts(job_text, url, title)
            pdf_path = artifacts["pdf_path"]
            markdown_path = artifacts["markdown_path"]
            job_text_path = artifacts["job_text_path"]
            log_job(url, title, score, "Drafted" if pdf_path else "Draft Failed")
            send_discord_notification(
                f"High Match ({score}): {title}\nURL: {url}\n"
                f"PDF: {pdf_path if pdf_path else 'generation failed'}\n"
                f"Markdown: {markdown_path if markdown_path else 'generation failed'}\n"
                f"Job Text: {job_text_path if job_text_path else 'capture failed'}"
            )
        else:
            log_job(url, title, score, "Rejected")
    except (requests.RequestException, ValueError, TypeError) as exc:
        logger.exception("Evaluation error for %s: %s", url, exc)
        log_job(url, "Evaluation Failed", 0, "Error")


def decode_text_part(part: Message) -> str:
    payload = part.get_payload(decode=True) or b""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def extract_links_from_message(msg: Message) -> list[str]:
    body_parts: list[str] = []
    html_parts: list[str] = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                body_parts.append(decode_text_part(part))
            elif part.get_content_type() == "text/html":
                html_parts.append(decode_text_part(part))
    else:
        if msg.get_content_type() == "text/html":
            html_parts.append(decode_text_part(msg))
        else:
            body_parts.append(decode_text_part(msg))

    combined_body = normalize_text("\n".join(body_parts))
    raw_links = re.findall(r"https?://[^\s]+", combined_body)

    for html in html_parts:
        soup = BeautifulSoup(html, "html.parser")
        html_text = normalize_text(soup.get_text("\n", strip=True))
        raw_links.extend(re.findall(r"https?://[^\s]+", html_text))
        for anchor in soup.find_all("a", href=True):
            raw_links.append(anchor["href"])

    cleaned_links: list[str] = []
    for link in raw_links:
        cleaned = link.rstrip(").,>\"'")
        if cleaned.startswith(("http://", "https://")) and cleaned not in cleaned_links:
            cleaned_links.append(cleaned)

    return cleaned_links


def fetch_job_emails() -> None:
    missing = validate_config()
    if missing:
        raise RuntimeError(
            "Missing required configuration values: "
            + ", ".join(missing)
            + ". Copy .env.example to .env, fill in the placeholders, and ensure the candidate profile file exists."
        )

    setup_database()
    mail: Optional[imaplib.IMAP4_SSL] = None

    try:
        def connect_mail() -> imaplib.IMAP4_SSL:
            client = imaplib.IMAP4_SSL(IMAP_SERVER)
            client.login(EMAIL_ACCOUNT, APP_PASSWORD)
            status, _ = client.select(INBOX_FOLDER)
            if status != "OK":
                raise imaplib.IMAP4.error(f"Could not select mailbox: {INBOX_FOLDER}")
            return client

        mail = retry(
            "IMAP connection",
            connect_mail,
            attempts=3,
            base_delay=2,
            retryable=(imaplib.IMAP4.error, OSError),
        )

        status, messages = mail.search(None, f'(UNSEEN FROM "{EMAIL_FROM_FILTER}")')
        if status != "OK":
            raise imaplib.IMAP4.error("Could not search the mailbox for unseen job emails")

        if not messages or not messages[0]:
            logger.info("No new messages found from %s.", EMAIL_FROM_FILTER)
            return

        extracted_links: list[str] = []
        for email_id in messages[0].split():
            status, msg_data = mail.fetch(email_id, "(RFC822)")
            if status != "OK":
                logger.warning("Skipping message %s because IMAP fetch failed.", email_id.decode("utf-8", errors="ignore"))
                continue
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    extracted_links.extend(extract_links_from_message(msg))

        unique_links = list(dict.fromkeys(extracted_links))
        logger.info("Found %s unique links to evaluate.", len(unique_links))

        for url in unique_links:
            if job_exists(url):
                logger.info("Skipping previously processed job: %s", url)
                continue

            job_text = extract_job_playwright(url)
            if job_text:
                evaluate_and_process(job_text, url)
            else:
                log_job(url, "Extraction Failed", 0, "Error")
            time.sleep(JOB_DELAY_SECONDS)
    except (imaplib.IMAP4.error, requests.RequestException, sqlite3.Error, RuntimeError, OSError) as exc:
        error_message = f"Agent Error: {exc}"
        logger.exception(error_message)
        send_discord_notification(error_message)
        raise
    finally:
        if mail is not None:
            with suppress(imaplib.IMAP4.error, OSError):
                mail.logout()


if __name__ == "__main__":
    fetch_job_emails()
