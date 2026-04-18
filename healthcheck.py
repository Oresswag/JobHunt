#!/usr/bin/env python3
"""Lightweight operational checks for the AI job agent environment."""

import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def check_module(name: str) -> str:
    return "ok" if importlib.util.find_spec(name) else "missing"


def check_path_exists(path_text: str) -> str:
    path = Path(path_text)
    if not path.is_absolute():
        path = (BASE_DIR / path).resolve()
    return "ok" if path.exists() else f"missing ({path})"


def check_ollama(url: str, model: str) -> str:
    try:
        request = Request(
            url,
            data=json.dumps(
                {
                    "model": model,
                    "prompt": 'Respond with JSON: {"status":"ok"}',
                    "stream": False,
                    "format": "json",
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(request, timeout=10) as response:
            if response.status != 200:
                return f"error (status {response.status})"
            payload = json.loads(response.read().decode("utf-8"))
            return "ok" if "response" in payload else "unexpected response"
    except URLError as exc:
        return f"unreachable ({exc.reason})"
    except Exception as exc:
        return f"error ({exc})"


def main() -> int:
    checks = {
        "python_module.requests": check_module("requests"),
        "python_module.playwright": check_module("playwright"),
        "python_module.markdown": check_module("markdown"),
        "python_module.pdfkit": check_module("pdfkit"),
        "python_module.bs4": check_module("bs4"),
        "python_module.dotenv": check_module("dotenv"),
        "binary.wkhtmltopdf": "ok" if (os.environ.get("WKHTMLTOPDF_PATH") or shutil.which("wkhtmltopdf")) else "missing",
        "config.env": "ok" if (BASE_DIR / ".env").exists() else "missing",
        "config.chrome_profile_dir": check_path_exists(os.environ.get("CHROME_PROFILE_DIR", "chrome-profile")),
        "config.candidate_profile": check_path_exists(os.environ.get("CANDIDATE_PROFILE_PATH", "candidate_profile.md")),
    }
    browser_path = os.environ.get("BROWSER_EXECUTABLE_PATH", "").strip()
    if browser_path:
        checks["config.browser_executable"] = check_path_exists(browser_path)

    llm_url = os.environ.get("LLM_API_URL", "").strip()
    model_name = os.environ.get("MODEL_NAME", "").strip()
    if llm_url and model_name:
        checks["service.llm_generate"] = check_ollama(llm_url, model_name)
    else:
        checks["service.llm_generate"] = "skipped (missing LLM_API_URL or MODEL_NAME)"

    failed = False
    for name, status in checks.items():
        print(f"{name}: {status}")
        if status != "ok" and not status.startswith("skipped"):
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
