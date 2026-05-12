import os
from dotenv import load_dotenv

load_dotenv()

# OpenAI
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# MySQL
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", 3306))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "job_agent")

# SMTP
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM = os.getenv("SMTP_FROM", SMTP_USER)

# Flask
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
DEBUG = os.getenv("FLASK_DEBUG", "true").lower() == "true"
