import os
from dotenv import load_dotenv

load_dotenv()

# On Render, persistent disk is mounted at /data. Locally, use project dir.
DATA_DIR = os.getenv("DATA_DIR", os.path.dirname(__file__))
os.makedirs(DATA_DIR, exist_ok=True)

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
APIFY_API_TOKEN = os.environ["APIFY_API_TOKEN"]  # still used by fetch_tweets.py
LINKEDIN_LI_AT = os.getenv("LINKEDIN_LI_AT", "")  # no longer used for feed
UNIPILE_API_KEY = os.environ["UNIPILE_API_KEY"]
UNIPILE_DSN = os.environ["UNIPILE_DSN"]
UNIPILE_ACCOUNT_ID = os.environ["UNIPILE_ACCOUNT_ID"]
TWITTER_AUTH_TOKEN = os.environ["TWITTER_AUTH_TOKEN"]
TWITTER_CT0 = os.environ["TWITTER_CT0"]

# Official X API v2 (OAuth 1.0a user context) — used for posting/replying.
# Replaces the scrape-based CreateTweet that hit X's automation block (226).
X_API_KEY = os.getenv("X_API_KEY", "")
X_API_SECRET = os.getenv("X_API_SECRET", "")
X_ACCESS_TOKEN = os.getenv("X_ACCESS_TOKEN", "")
X_ACCESS_TOKEN_SECRET = os.getenv("X_ACCESS_TOKEN_SECRET", "")

# LinkedIn profile identifier for fetching own posts (e.g. "ACoAA...")
LINKEDIN_PROFILE_ID = os.getenv("LINKEDIN_PROFILE_ID", "")

# Minimum likes for a post to be included
MIN_LIKES = 50

# Publishing: random delay between individual comments (seconds)
PUBLISH_DELAY_MIN = 60
PUBLISH_DELAY_MAX = 600

