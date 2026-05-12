"""
Apply Agent — uses Selenium (Chrome) to auto-apply to jobs on:
  LinkedIn (Easy Apply), Naukri, Indeed, Glassdoor

Strategy per portal:
  LinkedIn  — clicks "Easy Apply", fills form fields, attaches resume PDF, submits.
  Naukri    — clicks "Apply", fills profile details, uploads resume.
  Indeed    — clicks "Apply now" / "Easily Apply", fills form, uploads resume.
  Glassdoor — redirects to company ATS; logs result + opens page for manual review.

Returns an ApplyResult with status: success | captcha | manual | error
"""

import io
import os
import queue
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

import config
import db


# ── Result dataclass ──────────────────────────────────────────────────────────

@dataclass
class ApplyResult:
    status: str          # success | captcha | manual | error
    message: str = ""
    screenshot: Optional[bytes] = None
    events: list = field(default_factory=list)


# ── Chrome factory ────────────────────────────────────────────────────────────

def _make_driver(headless: bool = True) -> webdriver.Chrome:
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_experimental_option("excludeSwitches", ["enable-automation"])
    opts.add_experimental_option("useAutomationExtension", False)
    opts.add_argument("--window-size=1400,900")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=opts)
    driver.execute_script(
        "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
    )
    return driver


def _wait(driver, selector, by=By.CSS_SELECTOR, timeout=10):
    return WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((by, selector))
    )


def _click(driver, selector, by=By.CSS_SELECTOR, timeout=8):
    el = WebDriverWait(driver, timeout).until(
        EC.element_to_be_clickable((by, selector))
    )
    driver.execute_script("arguments[0].scrollIntoView({block:'center'});", el)
    el.click()
    return el


def _has_captcha(driver) -> bool:
    src = driver.page_source.lower()
    return any(k in src for k in ["captcha", "recaptcha", "hcaptcha", "robot check", "verify you are human"])


def _screenshot(driver) -> bytes:
    try:
        return driver.get_screenshot_as_png()
    except Exception:
        return b""


# ── Portal detectors ──────────────────────────────────────────────────────────

def _detect_portal(url: str) -> str:
    url = url.lower()
    if "linkedin.com" in url:
        return "linkedin"
    if "naukri.com" in url:
        return "naukri"
    if "indeed.com" in url:
        return "indeed"
    if "glassdoor.com" in url:
        return "glassdoor"
    return "unknown"


# ── Portal handlers ───────────────────────────────────────────────────────────

def _apply_linkedin(driver, app_data: dict, resume_path: str, emit) -> ApplyResult:
    """LinkedIn Easy Apply flow."""
    emit("Opening LinkedIn job page…")
    driver.get(app_data["job_url"])
    time.sleep(3)

    if _has_captcha(driver):
        return ApplyResult("captcha", "LinkedIn CAPTCHA detected — manual apply needed.", _screenshot(driver))

    # Check if logged in
    if "login" in driver.current_url or "authwall" in driver.current_url:
        return ApplyResult(
            "manual",
            "LinkedIn requires login. Use 'Copy & Open' to apply manually.",
            _screenshot(driver),
        )

    # Find Easy Apply button
    try:
        apply_btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR,
                "button.jobs-apply-button, .jobs-s-apply button, button[aria-label*='Easy Apply']"))
        )
        apply_btn.click()
        emit("Clicked Easy Apply…")
        time.sleep(2)
    except Exception:
        return ApplyResult("manual", "No Easy Apply button found — this job requires manual apply on LinkedIn.", _screenshot(driver))

    if _has_captcha(driver):
        return ApplyResult("captcha", "CAPTCHA appeared after clicking Apply.", _screenshot(driver))

    # Fill modal fields step by step (up to 5 pages)
    for step in range(6):
        time.sleep(1.5)
        src = driver.page_source

        # Upload resume if file input visible
        try:
            file_inputs = driver.find_elements(By.CSS_SELECTOR, "input[type='file']")
            for fi in file_inputs:
                if fi.is_displayed() or True:
                    fi.send_keys(resume_path)
                    emit("Uploaded resume PDF…")
                    time.sleep(1)
                    break
        except Exception:
            pass

        # Fill phone if empty
        try:
            phone_input = driver.find_element(By.CSS_SELECTOR, "input[id*='phoneNumber'], input[name*='phone']")
            if not phone_input.get_attribute("value"):
                phone_input.clear()
                phone_input.send_keys("9999999999")
        except Exception:
            pass

        # Answer yes/no radio questions (pick first option = usually "Yes")
        try:
            radios = driver.find_elements(By.CSS_SELECTOR, "input[type='radio']")
            for r in radios:
                if not r.is_selected():
                    driver.execute_script("arguments[0].click()", r)
                    break
        except Exception:
            pass

        # Click Next or Submit
        try:
            submit_btn = driver.find_element(By.CSS_SELECTOR,
                "button[aria-label='Submit application'], button[aria-label='Review your application']")
            submit_btn.click()
            emit("Submitted application!")
            time.sleep(2)
            return ApplyResult("success", "Applied via LinkedIn Easy Apply.", _screenshot(driver))
        except Exception:
            pass

        try:
            next_btn = driver.find_element(By.CSS_SELECTOR, "button[aria-label='Continue to next step']")
            next_btn.click()
            emit(f"Step {step+1}: moving to next page…")
            continue
        except Exception:
            pass

        # Look for any primary action button
        try:
            btns = driver.find_elements(By.CSS_SELECTOR, ".artdeco-button--primary")
            if btns:
                btns[-1].click()
                time.sleep(1.5)
                if "submitted" in driver.page_source.lower() or "application sent" in driver.page_source.lower():
                    return ApplyResult("success", "Applied via LinkedIn Easy Apply.", _screenshot(driver))
        except Exception:
            pass

    return ApplyResult("manual", "Reached multi-step form limit — please complete manually on LinkedIn.", _screenshot(driver))


def _apply_naukri(driver, app_data: dict, resume_path: str, emit) -> ApplyResult:
    """Naukri apply flow."""
    emit("Opening Naukri job page…")
    driver.get(app_data["job_url"])
    time.sleep(3)

    if _has_captcha(driver):
        return ApplyResult("captcha", "Naukri CAPTCHA detected.", _screenshot(driver))

    if "login" in driver.current_url:
        return ApplyResult("manual", "Naukri requires login — apply manually.", _screenshot(driver))

    try:
        apply_btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR,
                "button#apply-button, .apply-button, button[class*='apply'], a[class*='apply']"))
        )
        apply_btn.click()
        emit("Clicked Apply on Naukri…")
        time.sleep(2)
    except Exception:
        return ApplyResult("manual", "No Apply button found on Naukri — apply manually.", _screenshot(driver))

    if _has_captcha(driver):
        return ApplyResult("captcha", "CAPTCHA after clicking Apply on Naukri.", _screenshot(driver))

    # Upload resume
    try:
        fi = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
        )
        fi.send_keys(resume_path)
        emit("Uploaded resume to Naukri…")
        time.sleep(1.5)
    except Exception:
        pass

    # Submit
    try:
        sub = WebDriverWait(driver, 5).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[type='submit'], button.submit-btn"))
        )
        sub.click()
        time.sleep(2)
        emit("Submitted on Naukri!")
        return ApplyResult("success", "Applied on Naukri.", _screenshot(driver))
    except Exception:
        return ApplyResult("manual", "Filled form on Naukri — please complete submission manually.", _screenshot(driver))


def _apply_indeed(driver, app_data: dict, resume_path: str, emit) -> ApplyResult:
    """Indeed apply flow."""
    emit("Opening Indeed job page…")
    driver.get(app_data["job_url"])
    time.sleep(3)

    if _has_captcha(driver):
        return ApplyResult("captcha", "Indeed bot detection triggered.", _screenshot(driver))

    try:
        apply_btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR,
                "button#indeedApplyButton, a.indeed-apply-button, button[class*='apply']"))
        )
        apply_btn.click()
        emit("Clicked Apply on Indeed…")
        time.sleep(2)
    except Exception:
        return ApplyResult("manual", "No apply button found on Indeed — apply manually.", _screenshot(driver))

    if _has_captcha(driver):
        return ApplyResult("captcha", "CAPTCHA detected on Indeed.", _screenshot(driver))

    # Upload resume if shown
    try:
        fi = WebDriverWait(driver, 5).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
        )
        fi.send_keys(resume_path)
        emit("Uploaded resume on Indeed…")
        time.sleep(1.5)
    except Exception:
        pass

    # Walk through up to 4 form pages
    for step in range(5):
        time.sleep(1.5)
        # Continue / Next
        try:
            btn = driver.find_element(By.CSS_SELECTOR,
                "button[data-testid='continue-button'], button[type='submit']")
            label = btn.text.lower()
            btn.click()
            if "submit" in label or "apply" in label:
                emit("Submitted on Indeed!")
                time.sleep(2)
                return ApplyResult("success", "Applied on Indeed.", _screenshot(driver))
            emit(f"Indeed step {step+1}…")
        except Exception:
            break

    return ApplyResult("manual", "Partially completed Indeed form — please finish manually.", _screenshot(driver))


def _apply_glassdoor(driver, app_data: dict, resume_path: str, emit) -> ApplyResult:
    """Glassdoor — most jobs redirect to company ATS. Open the page and log it."""
    emit("Opening Glassdoor job page…")
    driver.get(app_data["job_url"])
    time.sleep(3)

    if _has_captcha(driver):
        return ApplyResult("captcha", "Glassdoor bot detection triggered.", _screenshot(driver))

    try:
        apply_btn = WebDriverWait(driver, 8).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR,
                "button[data-test='apply-button'], a[data-test='apply-button'], .apply-btn"))
        )
        apply_btn.click()
        emit("Clicked Apply on Glassdoor…")
        time.sleep(2)
        # Usually redirects to company ATS — capture new URL
        new_url = driver.current_url
        if new_url != app_data["job_url"]:
            return ApplyResult(
                "manual",
                f"Glassdoor redirected to company ATS: {new_url} — please complete there.",
                _screenshot(driver),
            )
    except Exception:
        pass

    return ApplyResult("manual", "Glassdoor job opened — complete your application on the company page.", _screenshot(driver))


def _apply_unknown(driver, app_data: dict, resume_path: str, emit) -> ApplyResult:
    """Generic fallback — look for common apply button patterns."""
    emit("Opening job page…")
    driver.get(app_data["job_url"])
    time.sleep(3)

    if _has_captcha(driver):
        return ApplyResult("captcha", "CAPTCHA detected on job page.", _screenshot(driver))

    try:
        apply_btn = WebDriverWait(driver, 6).until(
            EC.element_to_be_clickable((By.XPATH,
                "//*[contains(translate(text(),'APPLY','apply'),'apply') and (self::button or self::a)]"))
        )
        apply_btn.click()
        emit("Clicked Apply button…")
        time.sleep(2)
    except Exception:
        return ApplyResult("manual", "Could not find an Apply button — please apply manually.", _screenshot(driver))

    try:
        fi = WebDriverWait(driver, 4).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='file']"))
        )
        fi.send_keys(resume_path)
        emit("Uploaded resume…")
        time.sleep(1.5)
    except Exception:
        pass

    return ApplyResult("manual", "Opened apply form — complete submission manually.", _screenshot(driver))


# ── PDF generator (used to create resume PDF for upload) ─────────────────────

def _resume_to_pdf(resume_text: str) -> str:
    """Write resume text to a temp PDF file. Returns file path."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.lib.enums import TA_LEFT
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer

    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    tmp.close()

    doc = SimpleDocTemplate(tmp.name, pagesize=A4,
                            leftMargin=20*mm, rightMargin=20*mm,
                            topMargin=20*mm, bottomMargin=20*mm)
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle("body", parent=styles["Normal"], fontSize=10, leading=14, alignment=TA_LEFT)
    head_style = ParagraphStyle("h", parent=styles["Heading2"], fontSize=12, leading=16, spaceBefore=8)

    story = []
    for line in resume_text.splitlines():
        s = line.strip()
        if not s:
            story.append(Spacer(1, 4))
            continue
        if s.isupper() and len(s) < 60:
            story.append(Paragraph(s, head_style))
        else:
            safe = s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            story.append(Paragraph(safe, body_style))
    doc.build(story)
    return tmp.name


# ── Main ApplyRunner (background thread) ─────────────────────────────────────

class ApplyRunner:
    """Runs the apply flow in a background thread, emits events via queue."""

    def __init__(self, app_id: int):
        self.app_id = app_id
        self.event_queue: queue.Queue = queue.Queue()
        self._result: Optional[ApplyResult] = None

    def _emit(self, message: str):
        self.event_queue.put({"message": message, "done": False})

    def _done(self, result: ApplyResult):
        self._result = result
        self.event_queue.put({"message": result.message, "status": result.status, "done": True})

    def _run(self):
        app_data = db.get_application(self.app_id)
        if not app_data:
            self._done(ApplyResult("error", "Application not found."))
            return

        resume_text = app_data.get("resume_tailored") or app_data.get("resume_original") or ""
        if not resume_text:
            self._done(ApplyResult("error", "No resume text available. Run the agent first."))
            return

        # Write resume to temp PDF
        self._emit("Generating resume PDF…")
        try:
            resume_path = _resume_to_pdf(resume_text)
        except Exception as e:
            self._done(ApplyResult("error", f"Failed to generate resume PDF: {e}"))
            return

        portal = _detect_portal(app_data.get("job_url", ""))
        self._emit(f"Detected portal: {portal}. Launching browser…")

        db.add_event(self.app_id, "apply_started", f"Auto-apply started on {portal}", source="agent")
        db.update_application(self.app_id, {"apply_status": "in_progress"})

        driver = None
        try:
            driver = _make_driver(headless=True)

            if portal == "linkedin":
                result = _apply_linkedin(driver, app_data, resume_path, self._emit)
            elif portal == "naukri":
                result = _apply_naukri(driver, app_data, resume_path, self._emit)
            elif portal == "indeed":
                result = _apply_indeed(driver, app_data, resume_path, self._emit)
            elif portal == "glassdoor":
                result = _apply_glassdoor(driver, app_data, resume_path, self._emit)
            else:
                result = _apply_unknown(driver, app_data, resume_path, self._emit)

        except Exception as e:
            result = ApplyResult("error", f"Browser error: {e}")
        finally:
            if driver:
                try:
                    driver.quit()
                except Exception:
                    pass
            try:
                os.unlink(resume_path)
            except Exception:
                pass

        # Persist result
        status_map = {"success": "applied", "captcha": "pending", "manual": "pending", "error": "pending"}
        db.update_application(self.app_id, {
            "apply_status": result.status,
            "apply_method": portal,
            "apply_error": result.message if result.status not in ("success",) else "",
            "status": status_map.get(result.status, "pending"),
        })

        event_type_map = {
            "success": "applied",
            "captcha": "captcha_blocked",
            "manual": "manual_needed",
            "error": "apply_error",
        }
        db.add_event(
            self.app_id,
            event_type_map.get(result.status, "apply_error"),
            result.message,
            source="agent",
        )

        self._done(result)

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def get_result(self) -> Optional[ApplyResult]:
        return self._result
