import os

from dotenv import load_dotenv

load_dotenv()

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379")
QUEUE_NAME = os.getenv("QUEUE_NAME", "tasks-queue")
POLL_TIMEOUT_SECONDS = int(os.getenv("POLL_TIMEOUT_SECONDS", "5"))
