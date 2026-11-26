"""Business constants (same values as in the Flask app.py)."""
from decimal import Decimal

MIN_PASSWORD = 4  # demo value; use 8+ in a real system
MAX_LOGIN_ATTEMPTS = 5
LOCKOUT_MINUTES = 15
RESET_TOKEN_MINUTES = 30
DEFAULT_MEMBER_BALANCE = Decimal("100.00")
DEFAULT_SESSION_PRICE = Decimal("10.00")
DEMO_PASSWORD = "1234"
