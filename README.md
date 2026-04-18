# Autonomous AI Job Agent v2.0

This project is a local automation tool that:

- checks a mailbox for matching job-alert emails
- extracts job pages with Playwright
- scores jobs against your profile with a local LLM
- stores processed jobs in SQLite
- renders tailored resume drafts to Markdown and PDF
- sends notifications to Discord for strong matches or failures

The current design stays intentionally simple: one script, one SQLite database, one queue directory, and an optional `systemd` timer for scheduled runs.

## Project files

- `job_agent_v2.py`: the main runner
- `candidate_profile.md`: the profile/resume text used for evaluation and tailoring
- `setup_indeed_session.py`: one-time Playwright login bootstrap
- `healthcheck.py`: quick environment validation
- `systemd/job-agent.service` and `systemd/job-agent.timer`: optional scheduler templates

## Runtime dependencies

You need these available on the machine where the agent runs:

- Python 3.9+
- Chromium for Playwright
- `wkhtmltopdf` for PDF rendering
- a reachable local LLM endpoint compatible with the configured generate API

Install those with your platform's package manager.

Examples:

- Debian/Ubuntu: `python3-venv`, `wkhtmltopdf`
- Arch/CachyOS: `python`, `python-virtualenv`, `wkhtmltopdf`
- macOS (Homebrew): `python`, `wkhtmltopdf`

## Python setup

```bash
python3 -m venv venv
source venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env
```

If you use Ollama locally, also pull the model you want in `.env`:

```bash
ollama pull qwen2.5:7b-instruct
```

## Configure the environment

Edit `.env` before the first run.

Important values:

- `EMAIL_ACCOUNT`: your mailbox address
- `APP_PASSWORD`: your Gmail 16-digit App Password
- `DISCORD_WEBHOOK_URL`: where alerts should be posted
- `LLM_API_URL`: your local LLM generate endpoint
- `MODEL_NAME`: the model to use for evaluation and drafting
- `CANDIDATE_PROFILE_PATH`: path to the profile/resume text file
- `PLAYWRIGHT_NAV_TIMEOUT_MS`: page navigation timeout
- `PLAYWRIGHT_TEXT_TIMEOUT_MS`: content extraction timeout
- `LLM_REQUEST_TIMEOUT_SECONDS`: timeout for the LLM call
- `JOB_DELAY_SECONDS`: delay between processed links
- `BROWSER_EXECUTABLE_PATH`: optional full path to the exact browser binary you want Playwright to use

Common local LLM endpoints:

- Ollama: `http://127.0.0.1:11434/api/generate`
- Jan: `http://127.0.0.1:1337/api/generate`

## Candidate profile

The agent no longer hardcodes the profile in Python.

Update `candidate_profile.md` whenever your experience, tools, or job focus changes. The script reads that file at runtime, so you do not need to edit `job_agent_v2.py` just to change your profile.

## One-time browser session bootstrap

Before headless Playwright can reuse your Indeed cookies, run the bootstrap script once with a visible browser:

```bash
source venv/bin/activate
python setup_indeed_session.py
```

When Chromium opens:

1. log into Indeed manually
2. complete any CAPTCHA or verification prompts
3. come back to the terminal and press Enter

This saves the session into `chrome-profile/`.

If the browser you launch from the desktop works but the Playwright-managed browser does not, set `BROWSER_EXECUTABLE_PATH` in `.env` to the exact browser binary you want the scripts to use.

## Manual smoke test

Before enabling any timer, run the script once by hand:

```bash
source venv/bin/activate
python healthcheck.py
python job_agent_v2.py
```

Expected outputs:

- `jobs.db` is created automatically
- `queue/` receives captured job text, tailored Markdown, and PDFs for high-match jobs
- Discord receives notifications for strong matches and operational failures
- `job_agent.log` contains rotating logs for debugging

## Optional systemd timer

If you want a local heartbeat on a Linux machine, use the provided templates:

```bash
sudo cp systemd/job-agent.service /etc/systemd/system/job-agent.service
sudo cp systemd/job-agent.timer /etc/systemd/system/job-agent.timer
sudo sed -i "s|__USER__|$USER|g" /etc/systemd/system/job-agent.service
sudo sed -i "s|__APP_DIR__|$PWD|g" /etc/systemd/system/job-agent.service
sudo systemctl daemon-reload
sudo systemctl enable --now job-agent.timer
```

Useful checks:

```bash
systemctl list-timers --all | grep job-agent
journalctl -u job-agent.service -n 100 --no-pager
tail -f job_agent.log
```

## Notes

- Keep `.env` private. It contains credentials and webhook URLs.
- The agent skips duplicates based on URLs already logged in `jobs.db`.
- If the LLM returns JSON wrapped in markdown fences, the script now strips and parses it safely.
- If Playwright hangs on a page, the timeout is intentionally much shorter now so one bad page does not stall the whole run.
