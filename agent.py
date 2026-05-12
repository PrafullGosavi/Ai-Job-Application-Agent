"""
LangChain agent with tools for the full job-application pipeline.

Flow (run by AgentRunner in sequence):
  1. scrape_job_tool      – fetch JD text from URL
  2. analyze_resume_tool  – gap analysis between resume and JD
  3. rewrite_resume_tool  – rewrite resume bullets to match JD
  4. generate_cover_letter_tool – personalised cover letter
  5. send_email_tool      – send via SMTP (optional)
"""

import json
import re
import smtplib
import queue
import threading
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

import requests
from bs4 import BeautifulSoup
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI

import config


# ---------------------------------------------------------------------------
# Scraping helper
# ---------------------------------------------------------------------------

def _fetch_text(url: str) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        )
    }
    resp = requests.get(url, headers=headers, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)


# ---------------------------------------------------------------------------
# LangChain Tools
# ---------------------------------------------------------------------------

@tool
def scrape_job_tool(job_url: str) -> str:
    """Scrape a job posting URL and return the raw job description text."""
    try:
        raw = _fetch_text(job_url)
        return raw[:6000]
    except Exception as e:
        return f"ERROR scraping {job_url}: {e}"


@tool
def analyze_resume_tool(input_json: str) -> str:
    """
    Compare a resume against a job description.
    Input: JSON string with keys 'resume' and 'jd'.
    """
    try:
        data = json.loads(input_json)
        resume = data["resume"]
        jd = data["jd"]
    except (json.JSONDecodeError, KeyError) as e:
        return f"ERROR: invalid input – {e}"

    llm = ChatOpenAI(model="gpt-4o", temperature=0, api_key=config.OPENAI_API_KEY)
    prompt = f"""You are a professional resume analyst and ATS (Applicant Tracking System) expert.

JOB DESCRIPTION:
{jd[:3000]}

CANDIDATE RESUME:
{resume[:3000]}

Provide a structured analysis with:
1. MATCHING SKILLS – skills/keywords present in both.
2. MISSING SKILLS – important JD keywords absent from resume.
3. KEYWORD RECOMMENDATIONS – top 10 keywords to add.
4. ATS SCORE – Output exactly one line in this format: ATS_SCORE: XX% — one-sentence rationale.
   The score should reflect how well the resume would pass ATS filtering for this specific job."""

    response = llm.invoke(prompt)
    return response.content


def _extract_ats_score(analysis: str) -> Optional[int]:
    """Parse ATS_SCORE: XX% from analysis text, returns integer 0-100 or None."""
    match = re.search(r"ATS_SCORE:\s*(\d{1,3})%", analysis)
    if match:
        return min(100, int(match.group(1)))
    # Fallback: look for any "XX%" near "ATS" or "fit"
    match = re.search(r"(?:ATS|fit|score)[^\d]{0,20}(\d{1,3})%", analysis, re.IGNORECASE)
    if match:
        return min(100, int(match.group(1)))
    return None


@tool
def rewrite_resume_tool(input_json: str) -> str:
    """
    Rewrite a resume to better match a job description.
    Input: JSON string with keys 'resume', 'jd', and 'analysis'.
    """
    try:
        data = json.loads(input_json)
        resume = data["resume"]
        jd = data["jd"]
        analysis = data.get("analysis", "")
    except (json.JSONDecodeError, KeyError) as e:
        return f"ERROR: invalid input – {e}"

    llm = ChatOpenAI(model="gpt-4o", temperature=0.3, api_key=config.OPENAI_API_KEY)
    prompt = f"""You are an expert resume writer.

JOB DESCRIPTION:
{jd[:3000]}

ORIGINAL RESUME:
{resume[:3000]}

GAP ANALYSIS:
{analysis[:1500]}

Rewrite the resume to match the JD. Rules:
- Preserve all facts (dates, companies, degrees, titles).
- Use strong action verbs and quantified achievements.
- Incorporate recommended keywords naturally.
- Keep the same section structure.
- Do NOT invent fake experience.
- Output ONLY the rewritten resume text."""

    response = llm.invoke(prompt)
    return response.content


@tool
def generate_cover_letter_tool(input_json: str) -> str:
    """
    Generate a personalised cover letter.
    Input: JSON string with keys 'resume', 'jd', 'company', and 'role'.
    """
    try:
        data = json.loads(input_json)
        resume = data["resume"]
        jd = data["jd"]
        company = data.get("company", "the company")
        role = data.get("role", "the role")
    except (json.JSONDecodeError, KeyError) as e:
        return f"ERROR: invalid input – {e}"

    llm = ChatOpenAI(model="gpt-4o", temperature=0.5, api_key=config.OPENAI_API_KEY)
    prompt = f"""Write a compelling cover letter for:
- Role: {role}
- Company: {company}

JOB DESCRIPTION:
{jd[:2500]}

CANDIDATE RESUME:
{resume[:2500]}

Guidelines:
- 3-4 paragraphs, professional but warm.
- Opening: genuine enthusiasm for the role/company.
- Middle: 2-3 achievements that address JD requirements.
- Closing: clear call-to-action for interview.
- Do NOT start with "I am writing to...".
- Output ONLY the cover letter text."""

    response = llm.invoke(prompt)
    return response.content


@tool
def send_email_tool(input_json: str) -> str:
    """
    Send a cover letter email via SMTP.
    Input: JSON string with keys 'to_email', 'subject', 'body'.
    """
    try:
        data = json.loads(input_json)
        to_email = data["to_email"]
        subject = data["subject"]
        body = data["body"]
    except (json.JSONDecodeError, KeyError) as e:
        return f"ERROR: invalid input – {e}"

    if not config.SMTP_USER or not config.SMTP_PASSWORD:
        return "ERROR: SMTP credentials not configured"

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = config.SMTP_FROM
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            server.sendmail(config.SMTP_FROM, to_email, msg.as_string())
        return "SUCCESS"
    except Exception as e:
        return f"ERROR sending email: {e}"


# ---------------------------------------------------------------------------
# Agent runner – runs steps sequentially, emits SSE events via queue
# ---------------------------------------------------------------------------

class AgentRunner:
    """Runs the pipeline in a background thread, emits progress via a queue."""

    def __init__(self, job_url: str, resume_text: str):
        self.job_url = job_url
        self.resume_text = resume_text
        self.event_queue: queue.Queue = queue.Queue()
        self._result: Optional[dict] = None
        self._error: Optional[str] = None

    def _emit(self, step: str, message: str, done: bool = False):
        self.event_queue.put({"step": step, "message": message, "done": done})

    def _run(self):
        try:
            self._emit("start", "Agent started")

            # Step 1: Scrape
            self._emit("scrape", "Scraping job posting…")
            jd_text = scrape_job_tool.invoke({"job_url": self.job_url})
            if jd_text.startswith("ERROR"):
                raise RuntimeError(jd_text)
            self._emit("scrape", "Job description scraped successfully", done=True)

            # Step 2: Analyze
            self._emit("analyze", "Analyzing resume against JD…")
            analysis = analyze_resume_tool.invoke(
                {"input_json": json.dumps({"resume": self.resume_text, "jd": jd_text})}
            )
            self._emit("analyze", "Gap analysis complete", done=True)

            # Step 3: Rewrite resume
            self._emit("rewrite", "Rewriting resume to match JD…")
            tailored = rewrite_resume_tool.invoke(
                {"input_json": json.dumps({
                    "resume": self.resume_text, "jd": jd_text, "analysis": analysis
                })}
            )
            self._emit("rewrite", "Tailored resume ready", done=True)

            # Step 4: Cover letter
            self._emit("cover_letter", "Generating cover letter…")
            jd_lines = jd_text.splitlines()
            company = next(
                (l.strip() for l in jd_lines if l.strip() and len(l.strip()) < 60),
                "the company",
            )
            role = jd_lines[0].strip() if jd_lines else "the role"

            cover = generate_cover_letter_tool.invoke(
                {"input_json": json.dumps({
                    "resume": self.resume_text, "jd": jd_text,
                    "company": company, "role": role,
                })}
            )
            self._emit("cover_letter", "Cover letter generated", done=True)

            ats_score = _extract_ats_score(analysis)
            self._result = {
                "company": company,
                "role": role,
                "jd_text": jd_text,
                "resume_tailored": tailored,
                "cover_letter": cover,
                "analysis": analysis,
                "ats_score": ats_score,
            }
            self._emit("complete", "All steps complete!")

        except Exception as exc:
            self._error = str(exc)
            self._emit("error", f"Agent error: {exc}")

    def start(self):
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def get_result(self) -> Optional[dict]:
        return self._result

    def get_error(self) -> Optional[str]:
        return self._error
