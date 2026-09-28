"""Planted-bug benchmark.

Each case is a tiny repository: `base` files are committed on `main`, `head`
files on a feature branch (the "PR"). Lines ending in `# <BUG>` mark where the
planted defect is; the marker is stripped before the files are written, so the
agent never sees it. Cases with no marker are clean changes: any comment on
them is a false alarm.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Case:
    name: str
    title: str
    base: dict[str, str]
    head: dict[str, str]
    category: str = "clean"
    needs_context: bool = False  # bug only visible by looking at unchanged files
    expected: list[tuple[str, int]] = field(default_factory=list)  # filled by prepare()


MARKER = "# <BUG>"


def prepare(case: Case) -> Case:
    """Strip markers from head files and record (path, line) of each planted bug."""
    cleaned = {}
    for path, text in case.head.items():
        out = []
        for no, line in enumerate(text.splitlines(), start=1):
            if line.rstrip().endswith(MARKER):
                case.expected.append((path, no))
                line = line.rstrip()[: -len(MARKER)].rstrip()
            out.append(line)
        cleaned[path] = "\n".join(out) + "\n"
    case.head = cleaned
    return case


CASES: list[Case] = [
    Case(
        name="off_by_one_pagination",
        title="Add pagination helper",
        category="bug",
        base={"pager.py": "PAGE_SIZE = 20\n"},
        head={"pager.py": '''PAGE_SIZE = 20


def get_page(items: list, page: int) -> list:
    """Return the 1-indexed `page` of items."""
    start = page * PAGE_SIZE  # <BUG>
    return items[start:start + PAGE_SIZE]
'''},
    ),
    Case(
        name="sql_injection",
        title="Search users by name",
        category="security",
        base={"db.py": '''import sqlite3


def connect(path="app.db"):
    return sqlite3.connect(path)
'''},
        head={"db.py": '''import sqlite3


def connect(path="app.db"):
    return sqlite3.connect(path)


def find_users(conn, name: str):
    cur = conn.execute(f"SELECT id, email FROM users WHERE name = '{name}'")  # <BUG>
    return cur.fetchall()
'''},
    ),
    Case(
        name="missing_await",
        title="Fetch prices concurrently",
        category="concurrency",
        base={"prices.py": '''import asyncio


async def fetch_price(client, symbol: str) -> float:
    resp = await client.get(f"/price/{symbol}")
    return resp.json()["price"]
'''},
        head={"prices.py": '''import asyncio


async def fetch_price(client, symbol: str) -> float:
    resp = await client.get(f"/price/{symbol}")
    return resp.json()["price"]


async def portfolio_value(client, holdings: dict[str, int]) -> float:
    total = 0.0
    for symbol, qty in holdings.items():
        price = fetch_price(client, symbol)  # <BUG>
        total += price * qty
    return total
'''},
    ),
    Case(
        name="mutable_default",
        title="Tag helper for articles",
        category="bug",
        base={"tags.py": "\n"},
        head={"tags.py": '''def add_tag(tag: str, tags: list = []) -> list:  # <BUG>
    tags.append(tag.lower())
    return tags
'''},
    ),
    Case(
        name="none_dereference_cross_file",
        title="Show user email on profile page",
        category="bug",
        needs_context=True,
        base={
            "users.py": '''USERS = {1: {"name": "Asha", "email": "asha@example.com"}}


def find_user(user_id: int):
    """Return the user dict, or None if no such user exists."""
    return USERS.get(user_id)
''',
            "views.py": "from users import find_user\n",
        },
        head={
            "users.py": '''USERS = {1: {"name": "Asha", "email": "asha@example.com"}}


def find_user(user_id: int):
    """Return the user dict, or None if no such user exists."""
    return USERS.get(user_id)
''',
            "views.py": '''from users import find_user


def profile(user_id: int) -> str:
    user = find_user(user_id)
    return f"<h1>{user['name']}</h1><p>{user['email']}</p>"  # <BUG>
''',
        },
    ),
    Case(
        name="hardcoded_secret",
        title="Integrate payment gateway",
        category="security",
        base={"payments.py": "import httpx\n"},
        head={"payments.py": '''import httpx

PAYMENT_SECRET = "psk-live-8e41f0c2a97b4d3e9f15c6a0b2d8e7f3"  # <BUG>


def charge(amount_cents: int, token: str) -> dict:
    r = httpx.post("https://payments.example.com/v1/charges",
                   auth=(PAYMENT_SECRET, ""),
                   data={"amount": amount_cents, "currency": "inr", "source": token})
    r.raise_for_status()
    return r.json()
'''},
    ),
    Case(
        name="swallowed_exception",
        title="Make order processing more robust",
        category="error-handling",
        base={"orders.py": '''def process(order, gateway):
    gateway.charge(order.total, order.card)
    order.status = "paid"
    return True
'''},
        head={"orders.py": '''import logging

log = logging.getLogger(__name__)


def process(order, gateway):
    try:
        gateway.charge(order.total, order.card)
    except Exception:
        pass  # <BUG>
    order.status = "paid"
    return True
'''},
    ),
    Case(
        name="resource_leak",
        title="Count lines across log files",
        category="error-handling",
        base={"logs.py": "import glob\n"},
        head={"logs.py": '''import glob


def count_lines(pattern: str) -> int:
    total = 0
    for path in glob.glob(pattern):
        f = open(path)  # <BUG>
        total += sum(1 for _ in f)
    return total
'''},
    ),
    Case(
        name="identity_comparison",
        title="Filter active accounts",
        category="bug",
        base={"accounts.py": "\n"},
        head={"accounts.py": '''def active_accounts(accounts: list[dict]) -> list[dict]:
    result = []
    for acc in accounts:
        if acc["status"] is "active":  # <BUG>
            result.append(acc)
    return result
'''},
    ),
    Case(
        name="division_by_zero",
        title="Add error-rate metric",
        category="bug",
        base={"metrics.py": '''def load_requests(window):
    """Return the list of requests in the time window (may be empty)."""
    return list(window)
'''},
        head={"metrics.py": '''def load_requests(window):
    """Return the list of requests in the time window (may be empty)."""
    return list(window)


def error_rate(window) -> float:
    requests = load_requests(window)
    errors = [r for r in requests if r["status"] >= 500]
    return len(errors) / len(requests)  # <BUG>
'''},
    ),
    Case(
        name="path_traversal",
        title="Serve uploaded files",
        category="security",
        base={"files.py": 'import os\n\nUPLOAD_DIR = "/srv/uploads"\n'},
        head={"files.py": '''import os

UPLOAD_DIR = "/srv/uploads"


def read_upload(filename: str) -> bytes:
    """`filename` comes straight from the HTTP request query string."""
    with open(os.path.join(UPLOAD_DIR, filename), "rb") as f:  # <BUG>
        return f.read()
'''},
    ),
    Case(
        name="wrong_return_value",
        title="Apply discount codes",
        category="bug",
        base={"pricing.py": "\n"},
        head={"pricing.py": '''def apply_discount(price: float, percent: float) -> float:
    if not 0 <= percent <= 100:
        raise ValueError("percent must be between 0 and 100")
    discounted = price * (1 - percent / 100)
    return round(price, 2)  # <BUG>
'''},
    ),
    Case(
        name="breaking_signature_cross_file",
        title="Support multiple currencies in billing",
        category="bug",
        needs_context=True,
        base={
            "billing.py": '''def charge(user: str, amount: float) -> str:
    return f"charged {user} {amount:.2f} INR"
''',
            "checkout.py": '''from billing import charge


def checkout(user: str, cart_total: float) -> str:
    return charge(user, cart_total)
''',
        },
        head={
            "billing.py": '''def charge(user: str, amount: float, currency: str) -> str:  # <BUG>
    return f"charged {user} {amount:.2f} {currency}"
''',
            "checkout.py": '''from billing import charge


def checkout(user: str, cart_total: float) -> str:
    return charge(user, cart_total)
''',
        },
    ),
    # no bugs in these, any comment is a false alarm
    Case(
        name="clean_refactor",
        title="Extract tax calculation into a helper",
        base={"cart.py": '''def total(items):
    subtotal = sum(i["price"] * i["qty"] for i in items)
    return round(subtotal * 1.18, 2)
'''},
        head={"cart.py": '''GST_RATE = 0.18


def with_tax(amount: float) -> float:
    return round(amount * (1 + GST_RATE), 2)


def total(items):
    subtotal = sum(i["price"] * i["qty"] for i in items)
    return with_tax(subtotal)
'''},
    ),
    Case(
        name="clean_validation",
        title="Validate age input",
        base={"forms.py": '''def parse_age(raw: str) -> int:
    return int(raw)
'''},
        head={"forms.py": '''def parse_age(raw: str) -> int:
    try:
        age = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"age must be a whole number, got {raw!r}") from exc
    if not 0 <= age <= 150:
        raise ValueError(f"age out of range: {age}")
    return age
'''},
    ),
    Case(
        name="clean_context_manager",
        title="Read config safely",
        base={"config_loader.py": '''import json


def load(path):
    f = open(path)
    data = json.load(f)
    f.close()
    return data
'''},
        head={"config_loader.py": '''import json
from pathlib import Path


def load(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)
'''},
    ),
    Case(
        name="clean_feature",
        title="Add slugify helper",
        base={"text.py": "import re\n"},
        head={"text.py": '''import re


def slugify(title: str) -> str:
    """'Hello, World!' -> 'hello-world'"""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower())
    return slug.strip("-")
'''},
    ),
]
