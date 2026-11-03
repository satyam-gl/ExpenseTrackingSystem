"""
Expense Tracker - a simple Streamlit app.

Run with:  streamlit run app.py
Each user's data is saved in data/users/<username>/expenses.db (SQLite),
with daily backups next to it in backups/.
"""

import html
import io
import json
import math
import re
import urllib.request
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

from storage import Users

# ---------- Settings ----------
APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
INCOME_COLUMNS = ["Date", "Source", "Amount", "Note", "Account"]
TRANSFER_COLUMNS = ["Date", "From", "To", "Amount", "Note"]
ACCOUNT_TYPES = ["Bank", "Credit card", "Wallet", "Cash"]
NO_ACCOUNT = "—"
# How a recurring item is handled: added by itself, or listed as a bill to mark paid
RECURRING_MODES = {"auto": "Add automatically", "remind": "Remind me"}
BILL_NOTICE_DAYS = 7
RECEIPT_UPLOAD_TYPES = ["jpg", "jpeg", "png", "webp", "pdf"]
MAX_RECEIPT_MB = 10
INCOME_SOURCES = ["Salary", "Freelance", "Business", "Interest", "Gift", "Other"]
CURRENCY = "₹"
HOME_CURRENCY = "INR"
# Currencies you can pay in, with how amounts are written
FOREIGN_CURRENCIES = {
    "USD": "US$", "EUR": "€", "GBP": "£", "AED": "AED ", "SGD": "S$", "THB": "฿", "JPY": "¥", "AUD": "A$",
    "CAD": "C$", "CHF": "CHF ", "MYR": "RM ", "SAR": "SAR ", "NPR": "NPR ", "IDR": "Rp ", "HKD": "HK$",
    "NZD": "NZ$", "KRW": "₩", "CNY": "CN¥",
}
# Fixed to another currency, so their rate comes from that one: code -> (base, units per 1 base)
PEGGED_CURRENCIES = {"AED": ("USD", 3.6725), "SAR": ("USD", 3.75), "NPR": ("INR", 1.6)}
RATES_URL = "https://api.frankfurter.dev/v1/{day}?from={code}&to=" + HOME_CURRENCY
COLUMNS = ["Date", "Category", "Amount", "Payment", "Note", "Account"]
PAYMENT_METHODS = ["UPI", "Card", "Cash", "Net banking", "Other"]
# Used to guess categories for imported bank transactions (matched in lower case)
CATEGORY_KEYWORDS = {
    "Food": ["swiggy", "zomato", "restaurant", "cafe", "dominos", "mcdonald", "kfc", "starbucks"],
    "Groceries": ["bigbasket", "blinkit", "zepto", "dmart", "grocer", "instamart", "jiomart"],
    "Transport": ["uber", "ola", "rapido", "metro", "petrol", "fuel", "fastag", "parking"],
    "Rent": ["rent"],
    "Bills": ["electricity", "airtel", "jio", "vodafone", "recharge", "broadband", "gas bill", "water bill"],
    "Shopping": ["amazon", "flipkart", "myntra", "ajio", "nykaa", "meesho"],
    "Health": ["pharmacy", "apollo", "medplus", "hospital", "clinic", "1mg", "pharmeasy"],
    "Entertainment": ["netflix", "spotify", "hotstar", "prime video", "bookmyshow", "pvr", "inox"],
    "Travel": ["irctc", "makemytrip", "goibibo", "indigo", "air india", "oyo", "airbnb", "redbus"],
}
DEFAULT_SETTINGS = {
    "monthly_budget": 20000,
    "categories": [
        {"name": "Food", "icon": "🍔"},
        {"name": "Groceries", "icon": "🛒"},
        {"name": "Transport", "icon": "🚌"},
        {"name": "Rent", "icon": "🏠"},
        {"name": "Bills", "icon": "💡"},
        {"name": "Shopping", "icon": "🛍️"},
        {"name": "Health", "icon": "💊"},
        {"name": "Entertainment", "icon": "🎬"},
        {"name": "Travel", "icon": "✈️"},
        {"name": "Other", "icon": "📦"},
    ],
    "recurring": [],
    "accounts": [],
    "goals": [],
    "dismissed_alerts": [],
    "tax_categories": None,  # None until chosen: guessed from category names
    "category_rules": {},  # Learned from your corrections: shop word -> category
    "dismissed_subscriptions": [],  # Shop words you said aren't subscriptions
    "forecast_floor": 0,  # Warn when a bank/wallet/cash balance is forecast to drop below this
    "main_account": None,  # Forecast puts income, bills and spending without an account here
    "loans": [],
    "networth_goals": True,  # Count goal savings in net worth (off if that money sits in an account)
    "assets": [],  # Things you own outside your accounts: {"name", "value", "since" (ISO date or None)}
    "fx_last": {},  # Last exchange rate fetched per currency, used when offline: code -> [date, rate]
}
# Shown next to tax-relevant categories in the Year tab (matched in lower case). Not tax advice.
TAX_HINTS = {
    "rent": "HRA / 80GG",
    "health": "80D (health insurance, preventive check-ups)",
    "medical": "80D / 80DDB",
    "insurance": "80C (life) / 80D (health)",
    "donation": "80G",
    "charity": "80G",
    "education": "80C (tuition fees)",
    "school": "80C (tuition fees)",
    "loan": "24(b) home loan interest / 80E education loan",
}

st.set_page_config(page_title="Expense Tracker", page_icon="💰", layout="wide")


# ---------- Login ----------
users = Users(DATA_DIR)


def render_login() -> None:
    _, middle, _ = st.columns([1, 2, 1])
    with middle:
        st.title("💰 Expense Tracker")
        log_in, sign_up = st.tabs(["Log in", "Create account"])
        with log_in, st.form("login"):
            username = st.text_input("Username", key="login_user").strip().lower()
            password = st.text_input("Password", type="password", key="login_password")
            if st.form_submit_button("Log in", type="primary", width="stretch"):
                if users.check(username, password):
                    st.session_state["user"] = username
                    st.rerun()
                st.error("Wrong username or password.")
        with sign_up, st.form("signup"):
            first_user = users.count() == 0
            if first_user:
                st.info("You're the first user, so any expenses already saved will belong to this account.")
            username = st.text_input("Choose a username", key="signup_user").strip().lower()
            password = st.text_input("Password (at least 8 characters)", type="password", key="signup_password")
            repeat = st.text_input("Repeat password", type="password", key="signup_repeat")
            if st.form_submit_button("Create account", type="primary", width="stretch"):
                if password != repeat:
                    st.error("The passwords don't match.")
                elif error := users.create(username, password):
                    st.error(error)
                else:
                    moved, problems = users.adopt_unowned_data(username, APP_DIR) if first_user else ([], [])
                    st.session_state["user"] = username
                    st.session_state["toast"] = " ".join([f"Welcome, {username}!", *moved])
                    st.session_state["login_problems"] = problems
                    st.rerun()


if "user" not in st.session_state:
    render_login()
    st.stop()

# ---------- Data helpers ----------
store = users.store_for(st.session_state["user"])
for message in st.session_state.pop("login_problems", []):
    st.warning(message)
store.daily_backup()


def load_settings() -> dict:
    return store.load_settings(DEFAULT_SETTINGS)


def save_settings(settings: dict) -> None:
    store.save_settings(settings)


def load_data() -> pd.DataFrame:
    return store.load("expenses")


def save_data(df: pd.DataFrame) -> None:
    store.save("expenses", df)


def load_income() -> pd.DataFrame:
    return store.load("income")


def save_income(income: pd.DataFrame) -> None:
    store.save("income", income)


def money(x: float) -> str:
    return f"{CURRENCY}{x:,.0f}"


def flash(message: str) -> None:
    """Show a toast after the next rerun, then rerun."""
    st.session_state["toast"] = message
    st.rerun()


# ---------- Other currencies ----------
def foreign(code: str, amount: float) -> str:
    """'US$40.00'"""
    return f"{FOREIGN_CURRENCIES.get(code, code + ' ')}{amount:,.2f}"


@st.cache_data(ttl=6 * 3600, show_spinner=False)
def _fetch_rate(code: str, day: str) -> tuple[float, str]:
    """Rupees per 1 unit of `code` on `day` (or the last working day before it), from frankfurter.dev.

    Raises when the rate can't be fetched, so failures aren't cached.
    """
    with urllib.request.urlopen(RATES_URL.format(day=day, code=code), timeout=6) as response:
        data = json.load(response)
    return float(data["rates"][HOME_CURRENCY]), data["date"]


def exchange_rate(code: str, day: date) -> tuple[float, str] | None:
    """Rupees per 1 unit of `code` on `day`, with a note on where the rate came from. None if unknown."""
    if code in PEGGED_CURRENCIES:
        base, per_base = PEGGED_CURRENCIES[code]
        if base == HOME_CURRENCY:
            return 1 / per_base, "fixed rate"
        found = exchange_rate(base, day)
        return (found[0] / per_base, found[1]) if found else None
    try:
        rate, rate_day = _fetch_rate(code, min(day, date.today()).isoformat())
    except Exception:  # offline, or the service is down: fall back to the last rate we saw
        last = settings["fx_last"].get(code)
        if not last:
            return None
        return last[1], f"offline, using the {date.fromisoformat(last[0]):%d %b %Y} rate"
    if settings["fx_last"].get(code, [""])[0] < rate_day:
        settings["fx_last"][code] = [rate_day, rate]
        save_settings(settings)
    return rate, "" if rate_day == day.isoformat() else f"rate for {date.fromisoformat(rate_day):%d %b}"


def paid_in(code: str, amount: float, rate: float, note: str = "") -> str:
    """'US$40.00 at ₹94.95 (rate for 30 Oct)'"""
    return f"{foreign(code, amount)} at {CURRENCY}{rate:,.2f}" + (f" ({note})" if note else "")


settings = load_settings()
CATEGORIES = [c["name"] for c in settings["categories"]]
ICONS = {c["name"]: c["icon"] for c in settings["categories"]}
ACCOUNTS = [a["name"] for a in settings["accounts"]]


def label(category: str) -> str:
    return f"{ICONS.get(category, '📦')}  {category}"


TAG_PATTERN = re.compile(r"#([\w-]+)")


def tags_of(note: str) -> set[str]:
    """'Dinner #Goa-trip #office' -> {'goa-trip', 'office'}"""
    return {t.lower() for t in TAG_PATTERN.findall(note or "")}


def account_value(choice: str) -> str:
    return "" if choice == NO_ACCOUNT else choice


def account_column():
    """Account column for tables; hidden until the user has set up accounts."""
    return st.column_config.SelectboxColumn("Account", options=ACCOUNTS) if ACCOUNTS else None


def keyword_category(text: str) -> str | None:
    """Category from your learned rules, else from CATEGORY_KEYWORDS, if any word appears in the text."""
    text = text.lower()
    for word, category in sorted(settings["category_rules"].items(), key=lambda r: -len(r[0])):
        if category in CATEGORIES and re.search(rf"(?<!\w){re.escape(word)}(?!\w)", text):
            return category
    return next(
        (c for c, words in CATEGORY_KEYWORDS.items() if c in CATEGORIES and any(w in text for w in words)),
        None,
    )


# Words in notes and bank descriptions that never name the shop ("UPI/DR/412345/SWIGGY/YESB")
MERCHANT_NOISE = {
    "upi", "pos", "neft", "imps", "rtgs", "ach", "nach", "ecom", "txn", "ref", "payment", "pay", "paid", "to",
    "from", "by", "via", "the", "and", "for", "pvt", "ltd", "limited", "private", "india", "bank", "order", "bill",
    "invoice", "purchase", "debit", "credit", "card", "transfer", "trf", "inr", "mob", "ybl", "axl", "ibl",
    "okaxis", "oksbi", "okhdfcbank", "okicici", "yesb", "sbin", "utib", "icic", "hdfc", "kkbk",
    "phonepe", "gpay", "google", "paytm", "bhim",  # payment apps, not shops
}


def merchant_key(note: str) -> str | None:
    """The word that names the shop in a note: 'UPI/DR/412345/SWIGGY LIMITED' -> 'swiggy'."""
    words = re.findall(r"[a-z][a-z&']{2,}", TAG_PATTERN.sub(" ", note or "").lower())
    return next((w for w in words if w not in MERCHANT_NOISE), None)


def learn_categories(pairs) -> list[str]:
    """Remember the category you chose for each (note, category), when the rules would guess otherwise.

    Returns 'shop → category' for each new rule, to show the user.
    """
    learned = {}
    for note, category in pairs:
        word = merchant_key(note)
        if word and category in CATEGORIES and keyword_category(note) != category:
            settings["category_rules"][word] = category
            learned[word] = category
    if learned:
        save_settings(settings)
    return [f"{w} → {c}" for w, c in learned.items()]


def learned_note(learned: list[str]) -> str:
    return f" · Learned: {', '.join(learned)}" if learned else ""


# ---------- Quick add ("250 swiggy upi yesterday #goa") ----------
# Longer phrases first, so "credit card" wins over "card"
QUICK_PAYMENT_WORDS = {
    "net banking": "Net banking", "netbanking": "Net banking", "credit card": "Card", "debit card": "Card",
    "upi": "UPI", "gpay": "UPI", "phonepe": "UPI", "paytm": "UPI", "card": "Card", "visa": "Card",
    "mastercard": "Card", "rupay": "Card", "cash": "Cash",
}
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
# Whole month names or their short forms, so "marina" isn't March
MONTH_WORDS = "|".join(sorted(
    {*MONTHS, "january", "february", "march", "april", "june", "july", "august", "september", "sept", "october",
     "november", "december"}, key=len, reverse=True,
))
QUICK_CURRENCY_SYMBOLS = {
    "US$": "USD", "S$": "SGD", "A$": "AUD", "C$": "CAD", "HK$": "HKD", "NZ$": "NZD", "$": "USD", "€": "EUR",
    "£": "GBP", "¥": "JPY", "฿": "THB", "₩": "KRW",
}
QUICK_CURRENCY_WORDS = {
    **{code.lower(): code for code in FOREIGN_CURRENCIES},
    # Not "pound": "450 pound cake" is cake. "40 pounds", "£40" and "40 gbp" still work.
    "dollar": "USD", "dollars": "USD", "euro": "EUR", "euros": "EUR", "pounds": "GBP",
    "dirham": "AED", "dirhams": "AED", "baht": "THB", "yen": "JPY", "ringgit": "MYR", "riyal": "SAR",
    "riyals": "SAR",
}
# Left over around the parts we take out: "paid 250 for lunch via upi" -> "lunch"
FILLER_WORDS = {"paid", "spent", "for", "on", "via", "using", "by", "with", "from", "at", "in", "rs", "last"}


def _take(pattern: str, text: str) -> tuple[re.Match | None, str]:
    """First match of the pattern (case-insensitive), and the text with it cut out."""
    match = re.search(pattern, text, flags=re.IGNORECASE)
    return match, (text[:match.start()] + " " + text[match.end():] if match else text)


def _month_day(month: int, day: int, year: int | None, today: date) -> date | None:
    """A typed day and month; without a year, the most recent one that isn't in the future."""
    try:
        when = date(year or today.year, month, day)
    except ValueError:
        return None
    return when.replace(year=when.year - 1) if year is None and when > today else when


def _quick_date(text: str, today: date) -> tuple[date | None, str]:
    if (m := _take(r"\bday before yesterday\b", text))[0]:
        return today - timedelta(days=2), m[1]
    if (m := _take(r"\b(yesterday|yday)\b", text))[0]:
        return today - timedelta(days=1), m[1]
    if (m := _take(r"\btoday\b", text))[0]:
        return today, m[1]
    if (m := _take(r"\b(\d{1,2}) days? ago\b", text))[0]:
        return today - timedelta(days=int(m[0][1])), m[1]
    # Full weekday names anywhere; short ones only last or after "on"/"last", so "sun pharma" isn't a Sunday
    full = "|".join(WEEKDAYS)
    short = "|".join(d[:3] for d in WEEKDAYS)
    if (m := _take(rf"\b({full})\b|(?:\b(?:on|last)\s+)\b({short})\b|\b({short})\s*$", text))[0]:
        typed = next(g for g in m[0].groups() if g)
        weekday = next(i for i, d in enumerate(WEEKDAYS) if d.startswith(typed.lower()[:3]))
        return today - timedelta(days=(today.weekday() - weekday) % 7), m[1]
    if (m := _take(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2}|\d{4}))?\b", text))[0]:
        year = m[0][3] and int(m[0][3]) + (2000 if len(m[0][3]) == 2 else 0)
        return _month_day(int(m[0][2]), int(m[0][1]), year or None, today), m[1]
    if (m := _take(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({MONTH_WORDS})\.?(?!\w)", text))[0]:
        return _month_day(MONTHS.index(m[0][2].lower()[:3]) + 1, int(m[0][1]), None, today), m[1]
    if (m := _take(rf"\b({MONTH_WORDS})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text))[0]:
        return _month_day(MONTHS.index(m[0][1].lower()[:3]) + 1, int(m[0][2]), None, today), m[1]
    return today, text


def parse_quick_add(text: str, df: pd.DataFrame) -> dict:
    """Turn a line like '250 swiggy upi yesterday #goa' into an expense row.

    Amount is None when no amount was found; Date is None for an impossible date like 31/02.
    """
    today = date.today()
    when, rest = _quick_date(text, today)

    # Another currency: "$40", "€12.50", "40 usd", "200 dirhams"
    symbols = "|".join(re.escape(s) for s in sorted(QUICK_CURRENCY_SYMBOLS, key=len, reverse=True))
    found, rest = _take(rf"({symbols})\s*(?=\d)", rest)
    if found:
        currency = QUICK_CURRENCY_SYMBOLS[found[1].upper()]
    else:
        # Only next to a number ("40 pounds", "usd 40"), so "pound cake" stays a note
        words = "|".join(QUICK_CURRENCY_WORDS)
        found, rest = _take(rf"(?:(?<=\d)|(?<=\dk))\s*({words})\b|\b({words})\s*(?=\d)", rest)
        currency = QUICK_CURRENCY_WORDS[(found[1] or found[2]).lower()] if found else ""

    # Prefer an amount written with ₹/rs, else the biggest number ("2 pizzas 500"). "1.5k" means 1500.
    number = r"(\d[\d,]*(?:\.\d+)?)(k)?\b"

    def value(digits: str, k: str | None) -> float:
        return float(digits.replace(",", "")) * (1000 if k else 1)

    amount = None
    match, rest = _take(rf"(?:₹|\brs\.?|\binr)\s*{number}", rest)
    if match:
        amount = value(match[1], match[2])
    elif numbers := list(re.finditer(rf"(?<![\w.#])(-?){number}", rest)):
        match = max(numbers, key=lambda m: value(m[2], m[3]))
        rest = rest[:match.start()] + " " + rest[match.end():]
        amount = value(match[2], match[3]) * (-1 if match[1] else 1)  # negative is refused, not flipped

    # Accounts before payment words, so an account called "HDFC Card" isn't cut to "HDFC" + card
    account = ""
    for name in sorted(ACCOUNTS, key=len, reverse=True):
        found, rest = _take(rf"(?<!\w){re.escape(name)}(?!\w)", rest)
        if found:
            account = name
            break

    payment, rest = _take_payment(rest)
    if payment is None:
        # Whatever you pay with most often
        usual = df["Payment"].mode()
        payment = usual.iat[0] if not usual.empty else PAYMENT_METHODS[0]

    category = None
    for name in sorted(CATEGORIES, key=len, reverse=True):
        found, rest = _take(rf"(?<![\w#]){re.escape(name)}(?!\w)", rest)
        if found:
            category = name
            break

    words = rest.split()
    while words and words[0].lower() in FILLER_WORDS:
        words.pop(0)
    while words and words[-1].lower() in FILLER_WORDS:
        words.pop()
    note = " ".join(words)

    category = category or guess_category(note, df)
    # For another currency, Amount is what was paid in it until converted to rupees
    return {**dict(zip(COLUMNS, [when, category, amount, payment, note, account])), "Currency": currency,
            "Original": None}


def _take_payment(text: str) -> tuple[str | None, str]:
    """Payment method mentioned in the text, and the text with that word cut out."""
    for word, method in QUICK_PAYMENT_WORDS.items():
        found, rest = _take(rf"\b{re.escape(word)}\b", text)
        if found:
            return method, rest
    return None, text


def guess_category(note: str, df: pd.DataFrame, more_text: str = "") -> str:
    """Category from shop keywords, else what you used before for notes starting with the same word."""
    category = keyword_category(note) or keyword_category(more_text)
    if category is None and note.strip() and not df.empty:
        first = re.escape(note.split()[0].lower())
        past = df.loc[df["Note"].str.lower().str.match(rf"{first}\b", na=False), "Category"]
        past = past[past.isin(CATEGORIES)]
        category = past.mode().iat[0] if not past.empty else None
    return category or ("Other" if "Other" in CATEGORIES else CATEGORIES[0])


def quick_add(entry: dict, guessed: str) -> None:
    """Button callback: save the parsed expense and clear the box.

    A category typed in the text that differs from the guess is remembered for that shop.
    """
    store.add("expenses", entry)
    learned = learn_categories([(entry["Note"], entry["Category"])]) if entry["Category"] != guessed else []
    st.session_state["quick_text"] = ""
    st.session_state["toast"] = (
        f"Added {money(entry['Amount'])} for {label(entry['Category'])}"
        + (f" ({foreign(entry['Currency'], entry['Original'])})" if entry["Original"] else "")
        + learned_note(learned)
    )


# ---------- Receipt scanning ----------
# Lines holding the bill total, best first (spaces removed, since OCR often drops them)
TOTAL_WORDS = [
    "grandtotal", "nettotal", "netamount", "netpayable", "amountpayable", "totalpayable", "amountpaid",
    "totalamount", "billamount", "invoicetotal", "total",
]
NOT_TOTAL_WORDS = ["subtotal", "totalqty", "totalquantity", "totalitem", "totalsaving", "totaldiscount", "totaltax",
                   "totalgst"]
# Header lines that are not the shop's name, and words cut from the one that is
NOT_SHOP_WORDS = ["gst", "welcome", "original", "duplicate", "copy", "tel", "phone", "ph:", "www", "@", "date"]
SHOP_FILLER = re.compile(r"\b(tax invoice|invoice|receipt|bill|cash memo|order)\b", flags=re.IGNORECASE)
MONEY_PATTERN = re.compile(r"\d[\d,]*(?:\.\d{1,2})?(?![\d%.])")
MAX_SCAN_PIXELS = 2000  # Phone photos are shrunk to this size first, which keeps OCR quick


@st.cache_resource(show_spinner=False)
def ocr_engine():
    """The text recognizer, loaded once. None if rapidocr isn't installed."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return None
    return RapidOCR()


def _ocr_lines(image) -> list[str]:
    """Read an image's text, joining the pieces that sit on the same line."""
    import numpy as np
    from PIL import ImageOps

    image = ImageOps.exif_transpose(image).convert("RGB")
    image.thumbnail((MAX_SCAN_PIXELS, MAX_SCAN_PIXELS))
    found, _ = ocr_engine()(np.array(image))
    pieces = sorted(
        ((sum(p[1] for p in box) / 4, box[0][0], box[2][1] - box[0][1], text) for box, text, _ in found or []),
    )
    lines: list[list[tuple]] = []
    for piece in pieces:
        middle, _, height, _ = piece
        if lines and middle - lines[-1][0][0] < max(height, 1) / 2:
            lines[-1].append(piece)
        else:
            lines.append([piece])
    return ["  ".join(p[3] for p in sorted(line, key=lambda p: p[1])) for line in lines]


@st.cache_data(max_entries=20, show_spinner=False)
def receipt_lines(name: str, data: bytes) -> list[str] | None:
    """The text of a receipt photo or PDF, line by line. None if scanning isn't available."""
    if name.lower().endswith(".pdf"):
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(data)
        text = "\n".join(pdf[i].get_textpage().get_text_range() for i in range(min(len(pdf), 2)))
        if len(text.strip()) >= 20:  # A PDF with real text; otherwise it's a scan, so read the picture
            return [line.strip() for line in text.splitlines() if line.strip()]
        if ocr_engine() is None:
            return None
        return _ocr_lines(pdf[0].render(scale=2).to_pil())

    if ocr_engine() is None:
        return None
    from PIL import Image

    return _ocr_lines(Image.open(io.BytesIO(data)))


def _amount_on(line: str) -> float | None:
    numbers = MONEY_PATTERN.findall(line)
    return float(numbers[-1].replace(",", "")) if numbers else None


def _receipt_total(lines: list[str]) -> float | None:
    squashed = [re.sub(r"\s+", "", line.lower()) for line in lines]
    for word in TOTAL_WORDS:
        for i in reversed(range(len(lines))):  # Totals are near the bottom
            if word in squashed[i] and not any(w in squashed[i] for w in NOT_TOTAL_WORDS):
                # The amount is on the same line, or the next one
                amount = _amount_on(lines[i]) or (_amount_on(lines[i + 1]) if i + 1 < len(lines) else None)
                if amount:
                    return amount
    # No "total" line: the biggest amount with paise is usually it
    amounts = [float(n.replace(",", "")) for line in lines for n in MONEY_PATTERN.findall(line) if "." in n]
    return max(amounts, default=None)


def _receipt_date(text: str, today: date) -> date | None:
    months = "|".join(MONTHS)
    patterns = [
        (r"\b(\d{4})[/.-](\d{1,2})[/.-](\d{1,2})\b", lambda m: (m[1], m[2], m[3])),
        (r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})\b", lambda m: (m[3], m[2], m[1])),
        (rf"\b(\d{{1,2}})[\s-]*({months})[a-z]*[\s,'-]*(\d{{4}}|\d{{2}})\b", lambda m: (m[3], m[2], m[1])),
        (rf"\b({months})[a-z]*[\s-]*(\d{{1,2}}),?[\s-]*(\d{{4}})\b", lambda m: (m[3], m[1], m[2])),
    ]
    for pattern, parts in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            year, month, day = parts(match)
            year = int(year) + (2000 if len(year) == 2 else 0)
            month = MONTHS.index(month.lower()[:3]) + 1 if month.isalpha() else int(month)
            try:
                when = date(year, month, int(day))
            except ValueError:
                continue
            if today - timedelta(days=730) <= when <= today:
                return when
    return None


def _receipt_shop(lines: list[str]) -> str:
    for line in lines[:5]:
        if sum(ch.isdigit() for ch in line) > 1 or any(w in line.lower() for w in NOT_SHOP_WORDS):
            continue
        line = " ".join(SHOP_FILLER.sub(" ", line).split()).strip(" :-|")
        if sum(ch.isalpha() for ch in line) >= 3:
            return line.title() if line.isupper() else line
    return ""


def read_receipt(lines: list[str], df: pd.DataFrame) -> dict:
    """Best guesses for an expense from a receipt's text. Missing values are None."""
    text = "\n".join(lines)
    shop = _receipt_shop(lines)
    payment, _ = _take_payment(text)
    return {
        "Amount": _receipt_total(lines),
        "Date": _receipt_date(text, date.today()),
        "Category": guess_category(shop, df, text),
        "Payment": payment,
        "Note": shop,
    }


def scan_receipt(upload, df: pd.DataFrame) -> None:
    """Fill in the Add expense form from an uploaded receipt, and keep the file to attach."""
    st.session_state["scanned_id"] = upload.file_id  # set first, so a file that fails isn't read on every rerun
    # Forget the previous scan, so its file isn't attached if this one can't be read
    st.session_state.pop("scanned_receipt", None)
    st.session_state.pop("scan_category", None)
    try:
        with st.spinner("Reading the receipt…"):
            lines = receipt_lines(upload.name, upload.getvalue())
    except Exception:  # image and PDF libraries raise many kinds of errors for broken files
        st.session_state["scan_result"] = (
            "error", "Couldn't read this file. Is it a valid photo or PDF? You can still fill in the form yourself.", [],
        )
        return
    if lines is None:
        st.session_state["scan_result"] = (
            "error", "Receipt scanning isn't installed. Run: pip install rapidocr_onnxruntime pypdfium2", [],
        )
        return
    found = read_receipt(lines, df)
    fields = {"Amount": "form_amount", "Date": "form_date", "Category": "form_category", "Payment": "form_payment",
              "Note": "form_note"}
    for column, key in fields.items():
        if found[column]:
            st.session_state[key] = found[column]
    st.session_state["scanned_receipt"] = (upload.name, upload.getvalue())
    st.session_state["scan_category"] = found["Category"]
    missing = [c.lower() for c in ("Amount", "Date") if not found[c]]
    st.session_state["scan_result"] = (
        "warning" if missing else "success",
        "Filled in the form below. Check it, then click **Add expense**."
        + (f" Couldn't find the {' or '.join(missing)}, so please fill {'it' if len(missing) == 1 else 'them'} in."
           if missing else ""),
        lines,
    )


# ---------- Recurring expenses ----------
def first_due_month(day: int) -> str:
    """A new rule starts this month if its day hasn't passed yet, else next month."""
    this_month = pd.Period(date.today(), "M")
    return str(this_month if date.today().day <= day else this_month + 1)


def due_date(month: pd.Period, day: int) -> date:
    # Day 31 means "last day" in shorter months
    return date(month.year, month.month, min(day, month.days_in_month))


def next_month(rule: dict) -> pd.Period | None:
    """The month a rule is next due. A missing or broken one restarts from its next due date."""
    try:
        return pd.Period(rule["next"], "M") if isinstance(rule.get("next"), str) else None
    except ValueError:
        return None


def before_end(rule: dict, month: pd.Period) -> bool:
    """False after a rule's last month: loan EMIs stop once the loan is paid off."""
    return not rule.get("until") or month <= pd.Period(rule["until"], "M")


def apply_recurring(df: pd.DataFrame) -> pd.DataFrame:
    """Add every recurring expense that has come due since it was last added."""
    today = date.today()
    new_rows = []
    repaired = False
    for rule in settings["recurring"]:
        if next_month(rule) is None:
            rule["next"] = first_due_month(rule["day"])
            repaired = True
        if not rule["active"] or rule.get("mode") == "remind":
            continue
        month = next_month(rule)
        while due_date(month, rule["day"]) <= today and before_end(rule, month):
            new_rows.append([
                due_date(month, rule["day"]), rule["category"], rule["amount"],
                rule.get("payment", "Other"), f"🔁 {rule['name']}", rule.get("account", ""),
            ])
            month += 1
        rule["next"] = str(month)

    if not new_rows:
        if repaired:
            save_settings(settings)
        return df
    save_data(pd.concat([df, pd.DataFrame(new_rows, columns=COLUMNS)], ignore_index=True))
    save_settings(settings)
    st.session_state["toast"] = f"Added {len(new_rows)} recurring expense(s)"
    return load_data()


def upcoming_bills() -> list[tuple[int, dict, date]]:
    """Remind-me items due within the next week, plus any that are overdue."""
    horizon = date.today() + timedelta(days=BILL_NOTICE_DAYS)
    bills = []
    for i, rule in enumerate(settings["recurring"]):
        if rule["active"] and rule.get("mode") == "remind" and before_end(rule, pd.Period(rule["next"], "M")):
            due = due_date(pd.Period(rule["next"], "M"), rule["day"])
            if due <= horizon:
                bills.append((i, rule, due))
    return sorted(bills, key=lambda bill: bill[2])


# A payment counts as monthly when it comes this many days after the previous one
MONTHLY_GAP_DAYS = (25, 35)
SUBSCRIPTION_MIN_CHARGES = 3
SUBSCRIPTION_PRICE_WIGGLE = 0.15  # Recent charges may differ by this much (taxes, small price changes)


def monthly_streak(rows: pd.DataFrame, price_wiggle: float | None = None) -> int:
    """How many of the latest rows (sorted by Date) came a month apart, counting back from the last.

    With price_wiggle, each must also be within that share of the last row's Amount.
    """
    low, high = MONTHLY_GAP_DAYS
    dates, amounts = list(rows["Date"]), list(rows["Amount"])
    streak = 1
    for i in range(len(dates) - 1, 0, -1):
        if not low <= (dates[i] - dates[i - 1]).days <= high:
            break
        if price_wiggle is not None and abs(amounts[i - 1] - amounts[-1]) > amounts[-1] * price_wiggle:
            break
        streak += 1
    return streak


def still_monthly(last: date) -> bool:
    """A monthly payment last seen this recently hasn't stopped."""
    return (date.today() - last).days <= MONTHLY_GAP_DAYS[1] + 10


def find_subscriptions(df: pd.DataFrame) -> list[dict]:
    """Shops charging about the same amount every month that aren't set up as recurring yet."""
    known = {merchant_key(r["name"]) for r in settings["recurring"]} | set(settings["dismissed_subscriptions"])
    spent = df[~df["Note"].fillna("").str.startswith(("🔁", "🔔"))]  # already added by a recurring rule
    spent = spent.assign(Shop=spent["Note"].map(merchant_key)).dropna(subset=["Shop"])

    found = []
    for shop, rows in spent.groupby("Shop"):
        if shop in known or len(rows) < SUBSCRIPTION_MIN_CHARGES:
            continue
        rows = rows.sort_values("Date")
        last = rows.iloc[-1]
        if not still_monthly(last["Date"]):
            continue
        streak = monthly_streak(rows, SUBSCRIPTION_PRICE_WIGGLE)
        if streak < SUBSCRIPTION_MIN_CHARGES:
            continue
        recent = rows.tail(streak)
        day = int(pd.Series([d.day for d in recent["Date"]]).median())
        # The first due date a month after the last charge, so it isn't charged twice. Going by the calendar
        # month alone fails when a charge comes early: a day-1 charge on 31 Oct was November's.
        month = pd.Period(last["Date"], "M")
        while due_date(month, day) < last["Date"] + timedelta(days=MONTHLY_GAP_DAYS[0]):
            month += 1
        found.append({
            "shop": shop,
            "months": streak,
            "last": last["Date"],
            "rule": {
                "name": shop.title(),
                "category": last["Category"],
                "amount": float(last["Amount"]),
                "payment": last["Payment"] if last["Payment"] in PAYMENT_METHODS else "Other",
                "account": last["Account"] if last["Account"] in ACCOUNTS else "",
                "mode": "auto",
                "day": day,
                "active": True,
                "next": str(month),
            },
        })
    return sorted(found, key=lambda s: -s["rule"]["amount"])


def pay_bill(index: int) -> None:
    rule = settings["recurring"][index]
    store.add("expenses", {
        "Date": date.today(), "Category": rule["category"], "Amount": rule["amount"],
        "Payment": rule.get("payment", "Other"), "Note": f"🔔 {rule['name']}",
        "Account": rule.get("account", ""),
    })
    rule["next"] = str(pd.Period(rule["next"], "M") + 1)
    save_settings(settings)
    flash(f"Marked {rule['name']} as paid ({money(rule['amount'])})")


def smart_alerts(df: pd.DataFrame) -> list[tuple[str, str]]:
    """(id, message) for anything unusual recently. Dismissed ids are left out."""
    if df.empty:
        return []
    today = date.today()
    dates = pd.to_datetime(df["Date"])
    age = (pd.Timestamp(today) - dates).dt.days
    alerts = []

    # A category's last 7 days vs its weekly average over the 8 weeks before that
    recent = df[age.between(0, 6)].groupby("Category")["Amount"].sum()
    usual = df[age.between(7, 62)].groupby("Category")["Amount"].sum() / 8
    week = f"{today.isocalendar().year}-W{today.isocalendar().week}"
    for cat, spent in recent.items():
        typical = usual.get(cat, 0)
        if typical and spent >= 2 * typical and spent - typical >= 500:
            alerts.append((
                f"spike:{cat}:{week}",
                f"**{money(spent)}** on {label(cat)} in the last 7 days, "
                f"**{spent / typical:.1f}×** your usual {money(typical)} a week.",
            ))

    # Same date, amount and category more than once in the last 30 days
    last_month = df[age.between(0, 29)]
    repeats = last_month[last_month.duplicated(["Date", "Amount", "Category"], keep=False)]
    for (day, amount, cat), group in repeats.groupby(["Date", "Amount", "Category"]):
        alerts.append((
            f"dup:{day}:{amount}:{cat}",
            f"Possible duplicate: **{len(group)} × {money(amount)}** on {cat} on {day:%d %b}.",
        ))

    # A recent expense far above what that category usually costs
    for _, row in df[age.between(0, 6)].iterrows():
        history = df[(df["Category"] == row["Category"]) & (df["id"] != row["id"]) & (age > 6)]["Amount"]
        if len(history) >= 5 and row["Amount"] >= 3 * history.median() and row["Amount"] >= 1000:
            alerts.append((
                f"large:{row['id']}",
                f"Unusually large: **{money(row['Amount'])}** on {row['Category']} ({row['Date']:%d %b}), "
                f"vs a usual {money(history.median())}.",
            ))

    # Category budgets that are nearly used up with time still left
    month = pd.Period(today, "M")
    days_left = month.days_in_month - today.day
    spent_month = df[dates.dt.to_period("M") == month].groupby("Category")["Amount"].sum()
    for cat in settings["categories"]:
        limit = cat.get("budget")
        used = spent_month.get(cat["name"], 0) / limit if limit else 0
        if 0.8 <= used <= 1 and days_left > 5:
            alerts.append((
                f"budget:{cat['name']}:{month}",
                f"{label(cat['name'])} budget is **{used:.0%}** used with {days_left} days of the month left.",
            ))

    dismissed = set(settings["dismissed_alerts"])
    return [(key, text) for key, text in alerts if key not in dismissed]


def dismiss_alert(key: str) -> None:
    settings["dismissed_alerts"] = [*settings["dismissed_alerts"], key][-200:]
    save_settings(settings)
    flash("Alert dismissed")


df = apply_recurring(load_data())
income = load_income()
transfers = store.load("transfers")
contributions = store.load("contributions")
splits = store.load("splits").assign(Settled=lambda s: s["Settled"].fillna(0).astype(bool))

# ---------- Sidebar: add a new expense ----------
with st.sidebar:
    if "toast" in st.session_state:
        st.toast(st.session_state.pop("toast"), icon="✅")

    st.header("⚡ Quick add")
    quick = st.text_input(
        "Quick add", key="quick_text", label_visibility="collapsed",
        placeholder="e.g. 250 swiggy upi yesterday #goa",
        help="Type an amount and a few words, then press Enter. You can mention the category, "
             "payment method (upi, card, cash…), an account, a date (yesterday, monday, 5 nov, 05/11), "
             "#tags, and another currency ($40, €12, 40 usd).",
    )
    if quick.strip():
        entry = parse_quick_add(quick, df)
        rate = None
        if entry["Currency"] and (entry["Amount"] or 0) > 0 and entry["Date"]:
            with st.spinner("Getting the exchange rate…"):
                rate = exchange_rate(entry["Currency"], entry["Date"])
        if not entry["Amount"]:
            st.caption("Add an amount, like **250** or **₹1.5k**.")
        elif entry["Amount"] < 0:
            st.caption("Amounts can't be negative. To undo an expense, delete it in the **📋 Expenses** tab.")
        elif entry["Date"] is None:
            st.caption("That date doesn't exist. Try **5 nov** or **05/11**.")
        elif entry["Date"] > date.today():
            st.caption("That date is in the future.")
        elif entry["Currency"] and rate is None:
            st.caption(
                f"Couldn't get the {entry['Currency']} exchange rate (are you offline?). "
                "Use the form below and type the rate."
            )
        else:
            if rate:
                paid = paid_in(entry["Currency"], entry["Amount"], *rate)
                entry = {**entry, "Original": entry["Amount"], "Amount": round(entry["Amount"] * rate[0], 2)}
            details = [label(entry["Category"]), entry["Payment"], *filter(None, [entry["Account"]])]
            st.markdown(
                f"**{money(entry['Amount'])}** on "
                f"{entry['Date']:%a, %d %b}{'' if entry['Date'].year == date.today().year else entry['Date'].strftime(' %Y')}  \n"
                + (f"{paid}  \n" if rate else "")
                + " · ".join(details)
                + (f"  \n_{html.escape(entry['Note'])}_" if entry["Note"] else "")
            )
            st.button(
                "Add", type="primary", icon=":material/add:", width="stretch",
                on_click=quick_add, args=(entry, guess_category(entry["Note"], df)), key="quick_add",
            )
        st.caption("Something off? Change the text or use the form below.")

    st.header("➕ Add expense")
    with st.expander("📷 Scan a receipt", expanded="scanned_receipt" in st.session_state):
        scan = st.file_uploader(
            "Photo or PDF of a receipt", type=RECEIPT_UPLOAD_TYPES,
            key=f"scan_{st.session_state.get('scan_round', 0)}",
            help="Fills in the amount, date, shop, category and payment method for you to check.",
        )
        if scan is None:
            st.session_state.pop("scanned_receipt", None)
            st.session_state.pop("scan_result", None)
        elif scan.size > MAX_RECEIPT_MB * 1024 * 1024:
            st.error(f"Receipts can be up to {MAX_RECEIPT_MB} MB.")
        elif scan.file_id != st.session_state.get("scanned_id"):
            scan_receipt(scan, df)
        if result := st.session_state.get("scan_result"):
            kind, message, lines = result
            getattr(st, kind)(message)
            if lines:
                with st.popover("Text it read", width="stretch"):
                    st.code("\n".join(lines), language=None)

    # Not clear_on_submit: an error (no amount, no exchange rate) must keep what was typed.
    # A successful add empties the fields itself, below.
    with st.form("add_expense"):
        a1, a2 = st.columns([3, 2], vertical_alignment="bottom")
        amount = a1.number_input(
            "Amount", min_value=0.0, step=50.0, value=None, placeholder="e.g. 250", key="form_amount"
        )
        currency = a2.selectbox(
            "Currency", [HOME_CURRENCY, *FOREIGN_CURRENCIES], key="form_currency",
            help=f"Paid in another currency? It's converted to {CURRENCY} at the rate for the expense's date.",
        )
        with st.expander("Exchange rate (other currencies)"):
            manual_rate = st.number_input(
                f"{CURRENCY} per 1 unit", min_value=0.0, value=None, format="%.4f", key="form_rate",
                placeholder="Looked up for the date",
                help="Leave empty to look it up online. Type it to use your card's or exchange's actual rate.",
            )
        category = st.selectbox("Category", CATEGORIES, format_func=label, key="form_category")
        exp_date = st.date_input("Date", value="today", format="DD/MM/YYYY", key="form_date")
        payment = st.selectbox("Paid with", PAYMENT_METHODS, key="form_payment")
        account = st.selectbox("From account", [NO_ACCOUNT, *ACCOUNTS], key="form_account") if ACCOUNTS else NO_ACCOUNT
        note = st.text_input("Note", placeholder="What was it for? Add #tags (optional)", key="form_note")
        scanned = st.session_state.get("scanned_receipt")
        receipt = st.file_uploader(
            "Receipt (optional)", type=RECEIPT_UPLOAD_TYPES, key=f"form_receipt_{st.session_state.get('form_round', 0)}",
            help=f"The scanned {scanned[0]} is attached unless you choose another file." if scanned else None,
        )
        if scanned:
            st.caption(f"📎 {scanned[0]} will be attached")
        submitted = st.form_submit_button(
            "Add expense", type="primary", icon=":material/add:", width="stretch"
        )

    rate = None
    if submitted and amount and currency != HOME_CURRENCY:
        rate = (manual_rate, "") if manual_rate else exchange_rate(currency, exp_date)
    if submitted:
        if not amount:
            st.error("Enter an amount greater than 0.")
        elif receipt and receipt.size > MAX_RECEIPT_MB * 1024 * 1024:
            st.error(f"Receipts can be up to {MAX_RECEIPT_MB} MB.")
        elif currency != HOME_CURRENCY and rate is None:
            st.error(
                f"Couldn't get the {currency} exchange rate (are you offline?). "
                "Open **Exchange rate** in the form, type the rate and add it again."
            )
        else:
            values = [exp_date, category, amount, payment, note, account_value(account)]
            row = dict(zip(COLUMNS, values))
            if rate:
                row.update(Currency=currency, Original=amount, Amount=round(amount * rate[0], 2))
            amount = row["Amount"]
            expense_id = store.add("expenses", row)
            if receipt:
                store.save_receipt(expense_id, receipt.name, receipt.getvalue())
            elif scanned:
                store.save_receipt(expense_id, *scanned)
            # Changing the category the scan filled in teaches it for that shop
            scan_category = st.session_state.get("scan_category")
            learned = learn_categories([(note, category)]) if scanned and category != scan_category else []
            # Empty the form and the scan box for the next one
            for key in ("form_amount", "form_currency", "form_rate", "form_category", "form_date", "form_payment",
                        "form_note", "form_account", "scanned_receipt", "scan_result", "scanned_id", "scan_category"):
                st.session_state.pop(key, None)
            st.session_state["form_round"] = st.session_state.get("form_round", 0) + 1  # a new, empty receipt box
            if scanned:
                st.session_state["scan_round"] = st.session_state.get("scan_round", 0) + 1
            flash(
                f"Added {money(amount)} for {label(category)}"
                + (f" ({paid_in(currency, row['Original'], *rate)})" if rate else "")
                + learned_note(learned)
            )

    st.divider()
    st.subheader("📅 This month")
    today = date.today()
    monthly_budget = settings["monthly_budget"]
    spent_today = df.loc[df["Date"] == today, "Amount"].sum()
    this_month = pd.to_datetime(df["Date"]).dt.to_period("M") == pd.Period(today, "M")
    spent_month = df.loc[this_month, "Amount"].sum()
    left = monthly_budget - spent_month
    c1, c2 = st.columns(2)
    c1.metric("Today", money(spent_today))
    c2.metric(
        "Month",
        money(spent_month),
        delta=(f"{money(left)} left" if left >= 0 else f"-{money(-left)} over")
        if monthly_budget else None,
    )

    if alerts := smart_alerts(df):
        st.warning(f"🚨 {len(alerts)} alert{'s' if len(alerts) != 1 else ''}, see the Dashboard.")

    if bills := upcoming_bills():
        st.divider()
        st.subheader("🔔 Bills")
        for index, rule, due in bills:
            days = (due - today).days
            when = (
                f":red[**overdue by {-days} day{'s' if days != -1 else ''}**]" if days < 0
                else ":orange[**due today**]" if days == 0
                else f"due in {days} day{'s' if days != 1 else ''}"
            )
            text, button = st.columns([3, 2], vertical_alignment="center")
            text.markdown(f"**{rule['name']}** · {money(rule['amount'])}  \n{due:%d %b} · {when}")
            if button.button("Paid", key=f"pay_bill_{index}", width="stretch"):
                pay_bill(index)

    st.divider()
    who, out = st.columns([3, 2], vertical_alignment="center")
    who.caption(f"Signed in as **{st.session_state['user']}**")
    if out.button("Log out", width="stretch"):
        st.session_state.clear()
        st.rerun()


# ---------- Dashboard tab ----------
def render_category_budgets(month_df: pd.DataFrame) -> None:
    budgets = {c["name"]: c["budget"] for c in settings["categories"] if c.get("budget")}
    if not budgets:
        return

    st.subheader("Category budgets")
    spent = month_df.groupby("Category")["Amount"].sum()
    cols = st.columns(2)
    over = []
    for i, (cat, limit) in enumerate(budgets.items()):
        cat_spent = spent.get(cat, 0)
        used = cat_spent / limit
        status = "🔴" if used > 1 else "🟠" if used >= 0.8 else "🟢"
        cols[i % 2].progress(
            min(max(used, 0.0), 1.0),  # a negative amount (only from data edited outside the app) can't crash it
            text=f"{status} {label(cat)}: {money(cat_spent)} of {money(limit)} ({used:.0%})",
        )
        if used > 1:
            over.append(f"{cat} by {money(cat_spent - limit)}")
    if over:
        st.warning("Over category budget: " + ", ".join(over) + ".")


def spending_insights(month_df: pd.DataFrame, prev_df: pd.DataFrame, month: str) -> list[str]:
    """Plain-language observations about the selected month."""
    if month_df.empty:
        return []
    notes = []
    total = month_df["Amount"].sum()
    period = pd.Period(month, "M")
    today = date.today()

    # Days so far: the whole month if it's over, else up to today
    is_current = period == pd.Period(today, "M")
    days_elapsed = today.day if is_current else period.days_in_month

    if is_current and days_elapsed < period.days_in_month:
        projected = total / days_elapsed * period.days_in_month
        line = f"At this pace you'll spend about **{money(projected)}** this month"
        if monthly_budget:
            diff = projected - monthly_budget
            line += f", **{money(abs(diff))} {'over' if diff > 0 else 'under'}** your budget"
        notes.append(line + ".")

    by_cat = month_df.groupby("Category")["Amount"].sum().sort_values(ascending=False)
    if total > 0:  # only ₹0 expenses: no shares to work out
        notes.append(f"{label(by_cat.index[0])} is **{by_cat.iloc[0] / total:.0%}** of your spending.")

    biggest = month_df.loc[month_df["Amount"].idxmax()]
    note = f" ({biggest['Note']})" if biggest["Note"] else ""
    notes.append(
        f"Biggest expense: **{money(biggest['Amount'])}** on {biggest['Category']}{note}, "
        f"{biggest['Date']:%d %b}."
    )

    if not prev_df.empty:
        change = by_cat.sub(prev_df.groupby("Category")["Amount"].sum(), fill_value=0)
        cat = change.abs().idxmax()
        if change[cat]:
            notes.append(
                f"{cat} {'rose' if change[cat] > 0 else 'fell'} the most since last month "
                f"({'+' if change[cat] > 0 else '-'}{money(abs(change[cat]))})."
            )

    weekend = month_df.loc[pd.to_datetime(month_df["Date"]).dt.day_of_week >= 5, "Amount"].sum()
    if weekend:
        notes.append(f"Weekends account for **{weekend / total:.0%}** of your spending.")

    no_spend = days_elapsed - month_df.loc[pd.to_datetime(month_df["Date"]).dt.day <= days_elapsed, "Date"].nunique()
    if no_spend > 0:
        notes.append(f"You had **{no_spend}** no-spend day{'s' if no_spend != 1 else ''} so far.")
    return notes


def build_report(month: str, month_df: pd.DataFrame, month_income: float, notes: list[str]) -> str:
    """A self-contained, printable HTML summary of one month."""
    esc = html.escape
    total = month_df["Amount"].sum()
    cards = [("Total spent", money(total)), ("Transactions", str(len(month_df)))]
    if monthly_budget:
        cards.append(("Budget used", f"{total / monthly_budget:.0%} of {money(monthly_budget)}"))
    if month_income:
        saved = month_income - total
        cards += [("Income", money(month_income)), ("Saved", f"{saved / month_income:.0%} of income")]

    by_cat = month_df.groupby("Category")["Amount"].sum().sort_values(ascending=False)
    top = by_cat.iloc[0] if not by_cat.empty else 0
    cat_rows = "".join(
        f"<tr><td>{esc(label(c))}</td><td class='num'>{money(a)}</td><td class='num'>{a / total if total else 0:.0%}</td>"
        f"<td class='bar'><span style='width:{a / top * 100 if top else 0:.0f}%'></span></td></tr>"
        for c, a in by_cat.items()
    )
    txn_rows = "".join(
        f"<tr><td>{r.Date:%d %b}</td><td>{esc(r.Category)}</td><td>{esc(r.Payment)}</td>"
        f"<td>{esc(r.Note)}</td><td class='num'>{money(r.Amount)}</td></tr>"
        for r in month_df.sort_values("Date").itertuples()
    )
    def bold(text: str) -> str:
        # Insights use **bold** markdown; escape first, then turn it into <b>
        return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc(text))

    note_items = "".join(f"<li>{bold(n)}</li>" for n in notes)
    card_html = "".join(f"<div class='card'><small>{k}</small><b>{v}</b></div>" for k, v in cards)
    title = f"Expense report · {pd.Period(month, 'M'):%B %Y}"

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: system-ui, sans-serif; color: #1f2430; max-width: 860px; margin: 32px auto; padding: 0 16px; }}
  h1 {{ margin-bottom: 4px; }} h2 {{ margin-top: 32px; font-size: 18px; }}
  .muted {{ color: #6b7280; }}
  .cards {{ display: flex; flex-wrap: wrap; gap: 12px; margin-top: 16px; }}
  .card {{ border: 1px solid #e5e7eb; border-radius: 10px; padding: 12px 16px; min-width: 140px; }}
  .card small {{ display: block; color: #6b7280; }} .card b {{ font-size: 20px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  th, td {{ text-align: left; padding: 6px 8px; border-bottom: 1px solid #eef0f3; }}
  .num {{ text-align: right; white-space: nowrap; }}
  .bar {{ width: 35%; }} .bar span {{ display: block; height: 10px; border-radius: 5px; background: #6d5bd0; }}
  @media print {{ body {{ margin: 0; }} .bar span {{ -webkit-print-color-adjust: exact; print-color-adjust: exact; }} }}
</style></head><body>
<h1>{title}</h1>
<div class="muted">Generated {date.today():%d %b %Y} · amounts in {CURRENCY}</div>
<div class="cards">{card_html}</div>
{f"<h2>Insights</h2><ul>{note_items}</ul>" if note_items else ""}
<h2>By category</h2>
<table><tr><th>Category</th><th class="num">Amount</th><th class="num">Share</th><th></th></tr>{cat_rows}</table>
<h2>Transactions</h2>
<table><tr><th>Date</th><th>Category</th><th>Payment</th><th>Note</th><th class="num">Amount</th></tr>{txn_rows}</table>
</body></html>"""


def render_alerts(df: pd.DataFrame) -> None:
    alerts = smart_alerts(df)
    if not alerts:
        return
    with st.container(border=True):
        st.markdown("**🚨 Alerts**")
        for key, text in alerts:
            message, button = st.columns([6, 1], vertical_alignment="center")
            message.markdown(text)
            if button.button("Dismiss", key=f"dismiss_{key}", width="stretch"):
                dismiss_alert(key)


def current_or_before(newest_first: list, current) -> int:
    """Where a picker should start: the current period, or the latest one before it (not a future one)."""
    return next((i for i, option in enumerate(newest_first) if option <= current), 0)


def render_dashboard(df: pd.DataFrame) -> None:
    if df.empty:
        st.info("No expenses yet. Add your first one from the sidebar 👈")
        return

    render_alerts(df)
    if ACCOUNTS:
        for warning in forecast_warnings(cash_forecast(df, 30)[0]):
            st.warning(f"{warning} See the forecast in **🏦 Accounts**.", icon="🔮")

    df = df.assign(Month=pd.to_datetime(df["Date"]).dt.to_period("M").astype(str))
    months = sorted(df["Month"].unique(), reverse=True)
    pick_col, report_col = st.columns([4, 1], vertical_alignment="bottom")
    this_month = str(pd.Period(date.today(), "M"))
    selected_month = pick_col.selectbox("Month", months, index=current_or_before(months, this_month))
    month_df = df[df["Month"] == selected_month]

    prev_month = str(pd.Period(selected_month, "M") - 1)
    prev_df = df[df["Month"] == prev_month]

    total = month_df["Amount"].sum()
    days_with_spend = month_df["Date"].nunique()
    avg_day = total / days_with_spend if days_with_spend else 0
    top_cat = (
        month_df.groupby("Category")["Amount"].sum().idxmax() if not month_df.empty else "-"
    )
    prev_total = prev_df["Amount"].sum()
    prev_avg = prev_total / prev_df["Date"].nunique() if not prev_df.empty else 0

    def change(now: float, before: float) -> str | None:
        """Delta text vs last month; spending more is shown red (delta_color="inverse")."""
        if prev_df.empty:
            return None
        sign = "-" if now < before else "+"
        return f"{sign}{money(abs(now - before))} ({(now - before) / before:+.0%})" if before else f"+{money(now)}"

    st.caption(f"Changes are compared with {prev_month}." if not prev_df.empty else f"No data for {prev_month} to compare with.")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total spent", money(total), delta=change(total, prev_total), delta_color="inverse")
    c2.metric(
        "Transactions", len(month_df),
        delta=None if prev_df.empty else len(month_df) - len(prev_df), delta_color="off",
    )
    c3.metric("Avg per spending day", money(avg_day), delta=change(avg_day, prev_avg), delta_color="inverse")
    c4.metric("Top category", top_cat)

    month_income = income.loc[
        pd.to_datetime(income["Date"]).dt.to_period("M").astype(str) == selected_month, "Amount"
    ].sum()
    if not income.empty:
        saved = month_income - total
        i1, i2, i3, _ = st.columns(4)
        i1.metric("Income", money(month_income))
        i2.metric("Saved", money(saved) if saved >= 0 else f"-{money(-saved)}")
        i3.metric("Savings rate", f"{saved / month_income:.0%}" if month_income else "–")

    if monthly_budget > 0:
        used = total / monthly_budget
        st.progress(min(max(used, 0.0), 1.0), text=f"Budget used: {used:.0%} of {money(monthly_budget)}")
        if used > 1:
            st.warning(f"You are over budget by {money(total - monthly_budget)}.")

    notes = spending_insights(month_df, prev_df, selected_month)
    if notes:
        with st.container(border=True):
            st.markdown("**💡 Insights**\n\n" + "\n".join(f"- {n}" for n in notes))

    report_col.download_button(
        "📄 Download report",
        data=build_report(selected_month, month_df, month_income, notes).encode("utf-8"),
        file_name=f"expense-report-{selected_month}.html",
        mime="text/html",
        width="stretch",
        help="A printable summary of this month. Open it and use Print → Save as PDF for a PDF.",
    )

    render_category_budgets(month_df)

    if not prev_df.empty:
        st.subheader(f"Compared with {prev_month}")
        comp = pd.DataFrame({
            "Last month": prev_df.groupby("Category")["Amount"].sum(),
            "This month": month_df.groupby("Category")["Amount"].sum(),
        }).fillna(0)
        comp["Change"] = comp["This month"] - comp["Last month"]
        comp = comp.sort_values("Change", key=abs, ascending=False)
        st.dataframe(
            pd.DataFrame({
                "Category": comp.index.map(label),
                "Last month": comp["Last month"].map(money),
                "This month": comp["This month"].map(money),
                "Change": comp["Change"].map(
                    lambda x: f"▲ {money(x)}" if x > 0 else f"▼ {money(-x)}" if x < 0 else "–"
                ),
            }),
            hide_index=True,
            width="stretch",
        )

    left, right = st.columns(2)

    with left:
        st.subheader("By category")
        by_cat = month_df.groupby("Category", as_index=False)["Amount"].sum()
        chart = (
            alt.Chart(by_cat)
            .mark_bar()
            .encode(
                x=alt.X("Amount:Q", title=f"Amount ({CURRENCY})"),
                y=alt.Y("Category:N", sort="-x", title=None),
                tooltip=["Category", alt.Tooltip("Amount:Q", format=",.0f")],
            )
        )
        st.altair_chart(chart, width="stretch")

    with right:
        st.subheader("Daily spending")
        daily = month_df.groupby("Date", as_index=False)["Amount"].sum()
        daily["Date"] = pd.to_datetime(daily["Date"])
        chart = (
            alt.Chart(daily)
            .mark_line(point=True)
            .encode(
                x=alt.X("Date:T", title=None),
                y=alt.Y("Amount:Q", title=f"Amount ({CURRENCY})"),
                tooltip=[alt.Tooltip("Date:T"), alt.Tooltip("Amount:Q", format=",.0f")],
            )
        )
        st.altair_chart(chart, width="stretch")

    left, right = st.columns(2)

    with left:
        st.subheader("Monthly trend")
        by_month = df.groupby("Month", as_index=False)["Amount"].sum()
        st.bar_chart(by_month, x="Month", y="Amount")

    with right:
        st.subheader("By payment method")
        by_payment = month_df.groupby("Payment", as_index=False)["Amount"].sum()
        chart = (
            alt.Chart(by_payment)
            .mark_arc(innerRadius=60)
            .encode(
                theta="Amount:Q",
                color=alt.Color("Payment:N", sort=PAYMENT_METHODS, title=None),
                tooltip=["Payment", alt.Tooltip("Amount:Q", format=",.0f")],
            )
        )
        st.altair_chart(chart, width="stretch")


# ---------- Expenses tab: edit / delete ----------
def reset_editor() -> None:
    # Pending table edits refer to row positions, which change when the filters do
    st.session_state.pop("editor", None)


def render_expenses(df: pd.DataFrame) -> None:
    dates = pd.to_datetime(df["Date"])
    first, last = (dates.min().date(), dates.max().date()) if not df.empty else (date.today(),) * 2

    row_tags = df["Note"].map(tags_of)
    all_tags = sorted(set().union(*row_tags)) if not df.empty else []

    f1, f2, f3, f4, f5 = st.columns([3, 3, 2, 3, 2])
    search = f1.text_input("🔍 Search notes", placeholder="e.g. swiggy", on_change=reset_editor)
    cats = f2.multiselect("Categories", CATEGORIES, format_func=label, placeholder="All", on_change=reset_editor)
    tags = f3.multiselect(
        "Tags", all_tags, format_func=lambda t: f"#{t}", placeholder="Any", on_change=reset_editor,
        help="Add tags to any note, like 'Dinner #goa-trip #office'.",
    )
    date_range = f4.date_input(
        "Date range", value=(first, last), format="DD/MM/YYYY", on_change=reset_editor
    )
    min_amount = f5.number_input(f"Min amount ({CURRENCY})", min_value=0, step=100, on_change=reset_editor)

    mask = df["Amount"] >= min_amount
    if search:
        mask &= df["Note"].str.contains(search, case=False, regex=False)
    if cats:
        mask &= df["Category"].isin(cats)
    if tags:
        mask &= row_tags.map(lambda found: bool(found & set(tags)))
    if len(date_range) == 2:  # while picking, only the start date is set
        mask &= df["Date"].between(*date_range)
    shown = df[mask]

    if all_tags:
        with st.expander(f"🏷️ Totals by tag ({len(all_tags)})"):
            tagged = df.assign(Tag=row_tags.map(sorted)).explode("Tag").dropna(subset=["Tag"])
            summary = tagged.groupby("Tag").agg(
                Expenses=("Amount", "size"), Total=("Amount", "sum"),
                Categories=("Category", lambda c: ", ".join(sorted(set(c)))),
                From=("Date", "min"), To=("Date", "max"),
            ).sort_values("Total", ascending=False)
            st.dataframe(
                summary.reset_index().assign(
                    Tag=lambda s: "#" + s["Tag"],
                    Total=lambda s: s["Total"].map(money),
                    From=lambda s: s["From"].map(lambda d: f"{d:%d %b %Y}"),
                    To=lambda s: s["To"].map(lambda d: f"{d:%d %b %Y}"),
                ),
                hide_index=True,
                width="stretch",
            )

    st.caption(
        f"Showing **{len(shown)}** of {len(df)} expenses · Total **{money(shown['Amount'].sum())}**. "
        "Edit cells or select rows and press Delete, then click **Save changes**."
    )

    paid_in_column = [
        foreign(code, original) if code else "" for code, original in zip(shown["Currency"], shown["Original"])
    ]
    table = shown[["id", *COLUMNS]].assign(**{"📎": shown["Receipt"] != "", "Paid in": paid_in_column})
    if not any(paid_in_column):
        table = table.drop(columns="Paid in")
    edited = st.data_editor(
        table.sort_values("Date", ascending=False).reset_index(drop=True),
        num_rows="dynamic",
        width="stretch",
        hide_index=True,
        disabled=["📎", "Paid in"],
        column_config={
            "Date": st.column_config.DateColumn("Date", required=True, format="DD/MM/YYYY"),
            "Category": st.column_config.SelectboxColumn("Category", options=CATEGORIES, required=True),
            "Amount": st.column_config.NumberColumn(f"Amount ({CURRENCY})", min_value=0, format="%.2f", required=True),
            "Payment": st.column_config.SelectboxColumn("Payment", options=PAYMENT_METHODS, required=True),
            "Note": st.column_config.TextColumn("Note"),
            "Account": account_column(),
            "📎": st.column_config.CheckboxColumn("📎", help="Has a receipt", width="small"),
            "Paid in": st.column_config.TextColumn(
                "Paid in", help="What was actually paid, for expenses in another currency"
            ),
            "id": None,  # kept so edits stay linked to the same row, but not shown
        },
        key="editor",
    )

    col_save, col_download = st.columns([1, 1])
    if col_save.button("💾 Save changes", width="stretch"):
        # Receipts and the original currency aren't edited in the table, so carry them over by row id
        kept = df.set_index("id")
        edited = edited.drop(columns=["📎", "Paid in"], errors="ignore").assign(
            Receipt=edited["id"].map(kept["Receipt"]).fillna(""),
            Currency=edited["id"].map(kept["Currency"]).fillna(""),
            Original=edited["id"].map(kept["Original"]),
        )
        # Rows hidden by the filters are kept as they are; the shown ones are replaced by the edits
        save_data(pd.concat([df[~mask], edited], ignore_index=True))
        store.prune_receipts()
        # A changed category is remembered for that shop
        recategorized = edited[edited["Category"] != edited["id"].map(df.set_index("id")["Category"])]
        recategorized = recategorized[recategorized["id"].notna()]
        learned = learn_categories(zip(recategorized["Note"].fillna(""), recategorized["Category"]))
        reset_editor()
        flash("Expenses saved" + learned_note(learned))

    col_download.download_button(
        "⬇️ Download shown (CSV)",
        data=shown[COLUMNS].to_csv(index=False).encode("utf-8"),
        file_name="expenses.csv",
        mime="text/csv",
        width="stretch",
    )

    if not shown.empty:
        render_receipts(shown)


def render_receipts(shown: pd.DataFrame) -> None:
    st.subheader("📎 Receipts")
    rows = shown.sort_values("Date", ascending=False).set_index("id")
    expense_id = st.selectbox(
        "Expense",
        rows.index,
        format_func=lambda i: (
            f"{'📎 ' if rows.at[i, 'Receipt'] else ''}{rows.at[i, 'Date']:%d %b %Y} · {rows.at[i, 'Category']}"
            f" · {money(rows.at[i, 'Amount'])}" + (f" · {rows.at[i, 'Note']}" if rows.at[i, "Note"] else "")
        ),
        key="receipt_expense",
    )
    current = rows.at[expense_id, "Receipt"]
    if current:
        path = store.receipt_path(current)
        if not path.exists():
            st.warning("This receipt's file is missing.")
        elif path.suffix == ".pdf":
            st.download_button("⬇️ Open receipt (PDF)", data=path.read_bytes(), file_name=path.name, mime="application/pdf")
        else:
            try:
                st.image(str(path), width=360)
            except Exception:  # a damaged or mislabelled file shouldn't break the page
                st.warning("This receipt can't be shown as an image.")
                st.download_button("⬇️ Download receipt", data=path.read_bytes(), file_name=path.name)
        if st.button("🗑️ Remove receipt"):
            store.remove_receipt(int(expense_id))
            flash("Receipt removed")

    upload = st.file_uploader(
        "Replace receipt" if current else "Attach a receipt", type=RECEIPT_UPLOAD_TYPES,
        key=f"receipt_upload_{expense_id}",
    )
    if upload and st.button("📎 Save receipt", type="primary"):
        if upload.size > MAX_RECEIPT_MB * 1024 * 1024:
            st.error(f"Receipts can be up to {MAX_RECEIPT_MB} MB.")
        else:
            store.save_receipt(int(expense_id), upload.name, upload.getvalue())
            flash("Receipt saved")


# ---------- Settings tab: budget and categories ----------
def render_settings(df: pd.DataFrame) -> None:
    st.subheader("Monthly budget")
    budget = st.number_input(
        f"Monthly budget ({CURRENCY})", min_value=0, value=int(settings["monthly_budget"]), step=1000
    )

    st.subheader("Categories")
    st.caption(
        "Add, rename or remove categories. Renaming a category also updates its past expenses. "
        "Set a monthly budget per category, or leave it at 0 for no limit."
    )
    original = pd.DataFrame(settings["categories"], columns=["icon", "name", "budget"])
    original["budget"] = original["budget"].fillna(0).astype(int)
    # Each category's saved name, hidden: rows are matched by it, since an added row can reuse a deleted row's index
    original["was"] = original["name"]
    edited = st.data_editor(
        original,
        num_rows="dynamic",
        hide_index=True,
        column_config={
            "was": None,
            "icon": st.column_config.TextColumn("Icon", width="small", max_chars=2),
            "name": st.column_config.TextColumn("Name", required=True),
            "budget": st.column_config.NumberColumn(
                f"Monthly budget ({CURRENCY})", min_value=0, step=500, format="%d"
            ),
        },
        key="category_editor",
    )

    if not st.button("💾 Save settings", type="primary"):
        return

    edited["budget"] = edited["budget"].fillna(0).astype(int)
    edited = edited.fillna("")
    edited["name"] = edited["name"].astype(str).str.strip()
    edited = edited[edited["name"] != ""]
    names = list(edited["name"])

    # Rows kept from the original table still have their saved name, so a changed name is a rename
    still_there = edited[edited["was"] != ""]  # added rows have none (emptied by fillna above)
    renames = {was: name for was, name in zip(still_there["was"], still_there["name"]) if was != name}
    # A name typed again in an added row is kept; one taken over by renaming another row is still removed
    removed = set(original["name"]) - set(still_there["was"]) - set(edited.loc[edited["was"] == "", "name"])
    in_use = df["Category"].value_counts()
    in_recurring = {r["category"] for r in settings["recurring"]}

    if not names:
        st.error("Keep at least one category.")
    elif len({n.lower() for n in names}) != len(names):  # "food" next to "Food" would look like one category
        st.error("Category names must be unique (ignoring capital letters).")
    elif blocked := [
        f"{c} ({in_use[c]} expenses)" if c in in_use else f"{c} (recurring)"
        for c in removed if c in in_use or c in in_recurring
    ]:
        st.error(
            "These categories are still in use, so they can't be removed: "
            + ", ".join(blocked) + ". Rename them instead, or move their expenses first."
        )
    else:
        if renames:
            save_data(df.assign(Category=df["Category"].replace(renames)))
            for rule in settings["recurring"]:
                rule["category"] = renames.get(rule["category"], rule["category"])
            settings["category_rules"] = {w: renames.get(c, c) for w, c in settings["category_rules"].items()}
            if settings["tax_categories"] is not None:  # None until chosen in the Year tab
                settings["tax_categories"] = [renames.get(c, c) for c in settings["tax_categories"]]
        settings["monthly_budget"] = budget
        settings["categories"] = [
            {"name": r["name"], "icon": r["icon"].strip() or "📦", "budget": int(r["budget"])}
            for _, r in edited.iterrows()
        ]
        save_settings(settings)
        flash("Settings saved")


def render_learned_rules() -> None:
    st.subheader("🧠 Learned categories")
    st.caption(
        "When you change a guessed category (in Import, Quick add, a scanned receipt or the Expenses table), "
        "the app remembers it for that shop and uses it for future guesses. Edit, add or delete rules here."
    )
    rules = pd.DataFrame(sorted(settings["category_rules"].items()), columns=["word", "category"])
    edited = st.data_editor(
        rules,
        num_rows="dynamic",
        hide_index=True,
        column_config={
            "word": st.column_config.TextColumn(
                "Shop or word", required=True, help="Matched as a whole word in the note or bank description"
            ),
            "category": st.column_config.SelectboxColumn("Category", options=CATEGORIES, required=True),
        },
        key="rules_editor",
    )
    if st.button("💾 Save learned categories"):
        edited = edited.dropna()
        words = edited["word"].astype(str).str.strip().str.lower()
        settings["category_rules"] = {w: c for w, c in zip(words, edited["category"]) if w}
        save_settings(settings)
        flash("Learned categories saved")


# ---------- Income tab ----------
def render_income(df: pd.DataFrame) -> None:
    with st.form("add_income", clear_on_submit=True):
        c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 2, 3])
        amount = c1.number_input(f"Amount ({CURRENCY})", min_value=0.0, step=1000.0, value=None, placeholder="e.g. 50000")
        source = c2.selectbox("Source", INCOME_SOURCES)
        inc_date = c3.date_input("Date", value=date.today(), format="DD/MM/YYYY")
        account = c4.selectbox("Into account", [NO_ACCOUNT, *ACCOUNTS], disabled=not ACCOUNTS)
        note = c5.text_input("Note", placeholder="optional")
        submitted = st.form_submit_button("Add income", type="primary", icon=":material/add:")

    if submitted:
        if not amount:
            st.error("Enter an income amount greater than 0.")
        else:
            new_row = pd.DataFrame(
                [[inc_date, source, amount, note, account_value(account)]], columns=INCOME_COLUMNS
            )
            save_income(pd.concat([income, new_row], ignore_index=True))
            flash(f"Added income of {money(amount)} ({source})")

    if income.empty:
        st.info("No income recorded yet. Add your salary or other income above to see your savings rate.")
        return

    st.subheader("Monthly summary")
    by_month = pd.DataFrame({
        "Income": income.groupby(pd.to_datetime(income["Date"]).dt.to_period("M").astype(str))["Amount"].sum(),
        "Expenses": df.groupby(pd.to_datetime(df["Date"]).dt.to_period("M").astype(str))["Amount"].sum(),
    }).fillna(0).sort_index(ascending=False)
    by_month["Saved"] = by_month["Income"] - by_month["Expenses"]
    st.dataframe(
        pd.DataFrame({
            "Month": by_month.index,
            "Income": by_month["Income"].map(money),
            "Expenses": by_month["Expenses"].map(money),
            "Saved": by_month["Saved"].map(lambda x: money(x) if x >= 0 else f"-{money(-x)}"),
            "Savings rate": [f"{s / i:.0%}" if i else "–" for s, i in zip(by_month["Saved"], by_month["Income"])],
        }),
        hide_index=True,
        width="stretch",
    )

    st.subheader("All income")
    edited = st.data_editor(
        income.reset_index(drop=True),
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_config={
            "Date": st.column_config.DateColumn("Date", required=True, format="DD/MM/YYYY"),
            "Source": st.column_config.SelectboxColumn("Source", options=INCOME_SOURCES, required=True),
            "Amount": st.column_config.NumberColumn(f"Amount ({CURRENCY})", min_value=0, format="%.2f", required=True),
            "Note": st.column_config.TextColumn("Note"),
            "Account": account_column(),
            "id": None,  # kept so edits stay linked to the same row, but not shown
        },
        key="income_editor",
    )
    if st.button("💾 Save income"):
        save_income(edited)
        flash("Income saved")


# ---------- Import tab ----------
def parse_amount(text) -> float | None:
    """'₹1,250.00', 'Rs. 500', '-40', '300 Dr', '1,250.00-', '(250.00)' -> numbers. Dr and a minus or brackets
    mean money out (negative), Cr means money in. None if it isn't an amount."""
    s = str(text).strip().lower() if pd.notna(text) else ""
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    s = re.sub(r"₹|\b(?:rs|inr)\b\.?", "", s).strip()
    if marker := re.search(r"\s*\b(dr|cr)\b\.?$", s):
        negative |= marker[1] == "dr"
        s = s[:marker.start()]
    s = s.replace(",", "").replace(" ", "")
    if s.endswith("-"):
        negative, s = True, s[:-1]
    if s.startswith(("-", "+")):
        negative |= s[0] == "-"
        s = s[1:]
    if not re.fullmatch(r"\d+(?:\.\d+)?|\.\d+", s):
        return None
    return -float(s) if negative else float(s)


def parse_amounts(values: pd.Series) -> pd.Series:
    return values.map(parse_amount).astype(float)


def parse_dates(values: pd.Series) -> pd.Series:
    """Statement dates: day first (05/10/2026 is 5 Oct), except year-first ones like 2026-10-05."""
    year_first = values.fillna("").str.match(r"\s*\d{4}[-/.]\d{1,2}[-/.]\d{1,2}")
    dates = pd.to_datetime(values.where(~year_first), dayfirst=True, errors="coerce", format="mixed")
    if year_first.any():
        dates[year_first] = pd.to_datetime(values[year_first], errors="coerce", format="mixed")
    return dates


def render_import(df: pd.DataFrame) -> None:
    st.caption(
        "Import transactions from your bank or UPI app's statement export. "
        "You'll be able to review and fix everything before it's saved."
    )
    uploaded = st.file_uploader("Statement file (CSV)", type=["csv", "txt"])
    pasted = st.text_area(
        "…or paste rows copied from Excel / Google Sheets, including the header row", height=120
    )
    raw = uploaded.getvalue().decode("utf-8-sig", errors="replace") if uploaded else pasted
    if not raw.strip():
        return

    try:
        stmt = pd.read_csv(io.StringIO(raw), sep=None, engine="python", dtype=str).dropna(how="all")
    except (ValueError, pd.errors.ParserError) as e:
        st.error(f"Couldn't read that statement: {e}")
        return
    cols = list(stmt.columns)

    def guess(*words: str) -> int:
        return next((i for i, c in enumerate(cols) if any(w in str(c).lower() for w in words)), 0)

    c1, c2, c3, c4 = st.columns(4)
    date_col = c1.selectbox("Date column", cols, index=guess("date"))
    desc_col = c2.selectbox(
        "Description column", cols, index=guess("desc", "narration", "particular", "remark", "detail")
    )
    amount_col = c3.selectbox("Amount / debit column", cols, index=guess("debit", "withdrawal", "amount"))
    target = st.selectbox("Import into account", [NO_ACCOUNT, *ACCOUNTS]) if ACCOUNTS else NO_ACCOUNT
    spending = c4.selectbox(
        "Which rows are spending?",
        ["All rows", "Negative amounts", "Positive amounts"],
        help="Use 'Negative amounts' if your statement shows spending as -500 and income as +500.",
    )

    amounts = parse_amounts(stmt[amount_col])
    dates = parse_dates(stmt[date_col])
    # A blank amount is normal (e.g. a credit row in a separate debit column); one that can't be read isn't
    has_amount = stmt[amount_col].fillna("").str.strip() != ""
    unreadable = has_amount & (amounts.isna() | dates.isna())
    if unreadable.any():
        lines = [str(i + 2) for i in stmt.index[unreadable]]  # +2: the header is line 1
        st.warning(
            f"Skipped {len(lines)} row{'s' if len(lines) != 1 else ''} whose date or amount couldn't be read "
            f"(line{'s' if len(lines) != 1 else ''} {', '.join(lines[:10])}{'…' if len(lines) > 10 else ''}). "
            "Check those rows, or add them by hand."
        )
    keep = dates.notna() & amounts.notna() & (amounts != 0)
    if spending == "Negative amounts":
        keep &= amounts < 0
    elif spending == "Positive amounts":
        keep &= amounts > 0
    if not keep.any():
        st.warning("No spending rows found. Check the column choices above.")
        return

    # Reuse the category you gave the same note before, else fall back to keywords
    past = dict(zip(df["Note"].str.lower(), df["Category"]))
    fallback = "Other" if "Other" in CATEGORIES else CATEGORIES[0]

    def guess_category(text: str) -> str:
        text = text.lower()
        if past.get(text) in CATEGORIES:
            return past[text]
        return keyword_category(text) or fallback

    def guess_payment(text: str) -> str:
        text = text.lower()
        if "upi" in text:
            return "UPI"
        if any(w in text for w in ("pos", "card", "ecom")):
            return "Card"
        if any(w in text for w in ("atm", "cash")):
            return "Cash"
        if any(w in text for w in ("neft", "imps", "rtgs", "netbanking")):
            return "Net banking"
        return "Other"

    notes = stmt.loc[keep, desc_col].fillna("").str.strip().str[:100]
    preview = pd.DataFrame({
        "Date": dates[keep].dt.date,
        "Category": notes.map(guess_category),
        "Amount": amounts[keep].abs().round(2),
        "Payment": notes.map(guess_payment),
        "Note": notes,
        "Account": account_value(target),
    }).reset_index(drop=True)
    existing = set(zip(df["Date"], df["Amount"].round(2)))
    duplicate = [(d, a) in existing for d, a in zip(preview["Date"], preview["Amount"])]
    preview.insert(0, "Import", [not d for d in duplicate])
    preview["Status"] = ["⚠️ already added?" if d else "" for d in duplicate]

    st.caption(
        f"Found **{len(preview)}** spending rows. {sum(duplicate)} match an existing expense "
        "(same date and amount) and are unticked. Categories are guesses, so check them before importing."
    )
    edited = st.data_editor(
        preview,
        hide_index=True,
        width="stretch",
        disabled=["Status"],
        column_config={
            "Import": st.column_config.CheckboxColumn("Import"),
            "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
            "Category": st.column_config.SelectboxColumn("Category", options=CATEGORIES, required=True),
            "Amount": st.column_config.NumberColumn(f"Amount ({CURRENCY})", min_value=0, format="%.2f"),
            "Payment": st.column_config.SelectboxColumn("Payment", options=PAYMENT_METHODS, required=True),
            "Account": account_column(),
        },
        key="import_editor",
    )

    chosen = edited[edited["Import"]]
    if st.button(f"📥 Import {len(chosen)} expenses", type="primary", disabled=chosen.empty):
        incomplete = chosen["Date"].isna() | chosen["Amount"].isna() | (chosen["Amount"] <= 0)
        if incomplete.any():
            st.error(
                f"{incomplete.sum()} ticked row{'s are' if incomplete.sum() != 1 else ' is'} missing a date or amount. "
                "Fill them in or untick them, then import again."
            )
            return
        save_data(pd.concat([df, chosen[COLUMNS]], ignore_index=True))
        # Categories you changed from the guess are remembered for those shops
        fixed = chosen[chosen["Category"] != preview.loc[chosen.index, "Category"]]
        learned = learn_categories(zip(fixed["Note"], fixed["Category"]))
        flash(f"Imported {len(chosen)} expenses" + learned_note(learned))


# ---------- Recurring tab ----------
def render_subscriptions(df: pd.DataFrame) -> None:
    found = find_subscriptions(df)
    if not found:
        return
    monthly = sum(s["rule"]["amount"] for s in found)
    st.subheader("🔍 Possible subscriptions")
    st.caption(
        f"These charges came about the same amount every month, but aren't set up below. "
        f"Together they're **{money(monthly)} a month** ({money(monthly * 12)} a year). "
        "Add one to have it entered automatically each month, or hide it if it isn't a subscription."
    )
    for s in found:
        rule = s["rule"]
        with st.container(border=True):
            text, add, hide = st.columns([4, 1.5, 1.5], vertical_alignment="center")
            text.markdown(
                f"**{rule['name']}** · {money(rule['amount'])} a month · {label(rule['category'])}  \n"
                f"Around day {rule['day']} · {s['months']} months in a row, last on {s['last']:%d %b} · "
                f"{money(rule['amount'] * 12)} a year"
            )
            if add.button("Add to recurring", key=f"sub_add_{s['shop']}", icon=":material/add:", width="stretch"):
                settings["recurring"] = [*settings["recurring"], rule]
                save_settings(settings)
                flash(f"{rule['name']} added to recurring")
            if hide.button("Not a subscription", key=f"sub_hide_{s['shop']}", width="stretch"):
                settings["dismissed_subscriptions"] = [*settings["dismissed_subscriptions"], s["shop"]]
                save_settings(settings)
                flash(f"Won't suggest {rule['name']} again")
    st.divider()


def render_recurring() -> None:
    st.caption(
        "Expenses that repeat every month, on their day of the month (31 = last day). "
        "**Add automatically** suits fixed costs like rent or subscriptions. **Remind me** suits bills "
        "you pay yourself: they show in the sidebar from a week before they're due until you mark them paid. "
        "Untick **Active** to pause one."
    )
    original = pd.DataFrame(
        settings["recurring"],
        columns=["name", "category", "amount", "payment", "account", "mode", "day", "active", "next"],
    )
    # Rules saved before a field existed leave an all-empty (numeric) column, so make it text first
    original["payment"] = original["payment"].astype(object).fillna("Other")
    original["account"] = original["account"].astype(object).fillna("")
    original["mode"] = original["mode"].astype(object).fillna("auto").map(RECURRING_MODES)
    edited = st.data_editor(
        original,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_config={
            "name": st.column_config.TextColumn("Name", required=True),
            "category": st.column_config.SelectboxColumn("Category", options=CATEGORIES, required=True),
            "amount": st.column_config.NumberColumn(f"Amount ({CURRENCY})", min_value=0, format="%.2f", required=True),
            "payment": st.column_config.SelectboxColumn("Payment", options=PAYMENT_METHODS, default="UPI"),
            "account": account_column(),
            "mode": st.column_config.SelectboxColumn(
                "Mode", options=list(RECURRING_MODES.values()), default=RECURRING_MODES["auto"], required=True
            ),
            "day": st.column_config.NumberColumn("Day of month", min_value=1, max_value=31, step=1, format="%d", required=True),
            "active": st.column_config.CheckboxColumn("Active", default=True),
            "next": st.column_config.TextColumn("Next month due", disabled=True),
        },
        key="recurring_editor",
    )

    if not st.button("💾 Save recurring", type="primary"):
        return

    rules, problems = [], []
    ends = {rule["name"]: rule["until"] for rule in settings["recurring"] if rule.get("until")}  # not in the table
    for row_no, (i, r) in enumerate(edited.iterrows(), start=1):
        name = str(r["name"]).strip() if pd.notna(r["name"]) else ""
        if not name and pd.isna(r["amount"]):
            continue  # blank row
        if not name or pd.isna(r["amount"]) or r["amount"] <= 0 or pd.isna(r["day"]) \
                or r["category"] not in CATEGORIES:
            problems.append(f"row {row_no}")
            continue
        day = int(r["day"])
        active = bool(r["active"]) if pd.notna(r["active"]) else True
        # A table row added next to a deleted one can reuse its index, so tell new rows apart by their
        # "next" month: it's read-only, so only rows that were never saved have none
        is_new = i not in original.index or not isinstance(r["next"], str) or not r["next"]
        resumed = not is_new and active and not original.at[i, "active"]
        rules.append({
            "name": name,
            "category": r["category"],
            "amount": float(r["amount"]),
            "payment": r["payment"] if r["payment"] in PAYMENT_METHODS else "Other",
            "account": r["account"] if r["account"] in ACCOUNTS else "",
            "mode": "remind" if r["mode"] == RECURRING_MODES["remind"] else "auto",
            "day": day,
            "active": active,
            # New or resumed rules start from their next due date, so nothing is back-filled
            "next": first_due_month(day) if is_new or resumed else r["next"],
            **({"until": ends[name]} if name in ends else {}),
        })

    if problems:
        st.error(
            "Each recurring expense needs a name, category, amount above 0 and day. Check "
            + ", ".join(problems) + "."
        )
        return
    settings["recurring"] = rules
    save_settings(settings)
    flash("Recurring expenses saved")


# ---------- Year tab ----------
def tax_hint(category: str) -> str:
    # Whole words (plurals too), so "Parental care" isn't rent and "Current bills" isn't rent either
    return next(
        (hint for word, hint in TAX_HINTS.items() if re.search(rf"\b{word}(?:s|es)?\b", category.lower())), ""
    )


def save_tax_categories() -> None:
    settings["tax_categories"] = st.session_state["tax_categories"]
    save_settings(settings)


def render_year(df: pd.DataFrame) -> None:
    if df.empty and income.empty:
        st.info("Add some expenses or income to see your year.")
        return

    financial = st.radio(
        "View", ["Financial year (Apr–Mar)", "Calendar year"], horizontal=True, key="year_kind"
    ).startswith("Financial")

    def year_of(dates: pd.Series) -> pd.Series:
        dates = pd.to_datetime(dates)
        return dates.dt.year - (dates.dt.month < 4) if financial else dates.dt.year

    years = sorted(set(year_of(df["Date"])) | set(year_of(income["Date"])), reverse=True)
    this_year = int(year_of(pd.Series([date.today()])).iat[0])
    year = st.selectbox(
        "Year", years, key="year_pick", index=current_or_before(years, this_year),
        format_func=lambda y: f"FY {y}-{str(y + 1)[-2:]}" if financial else str(y),
    )
    start = date(year, 4, 1) if financial else date(year, 1, 1)
    end = date(year + 1, 3, 31) if financial else date(year, 12, 31)
    year_df = df[df["Date"].between(start, end)]
    year_income = income[income["Date"].between(start, end)]

    spent, earned = year_df["Amount"].sum(), year_income["Amount"].sum()
    months_so_far = min(12, max(1, (min(date.today(), end).year - start.year) * 12
                                + min(date.today(), end).month - start.month + 1))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Spent in year", money(spent))
    c2.metric("Monthly average", money(spent / months_so_far))
    if earned:
        c3.metric("Income in year", money(earned))
        c4.metric("Saved in year", signed_money(earned - spent), delta=f"{(earned - spent) / earned:.0%} of income", delta_color="off")

    st.subheader("Month by month")
    months = pd.period_range(start, end, freq="M")
    by_month = pd.DataFrame({
        "Expenses": year_df.groupby(pd.to_datetime(year_df["Date"]).dt.to_period("M"))["Amount"].sum(),
        "Income": year_income.groupby(pd.to_datetime(year_income["Date"]).dt.to_period("M"))["Amount"].sum(),
    }).reindex(months).fillna(0)
    long = by_month.reset_index(names="Month").melt("Month", var_name="Type", value_name="Amount")
    long["Month"] = long["Month"].dt.strftime("%b %y")
    st.altair_chart(
        alt.Chart(long).mark_bar().encode(
            x=alt.X("Month:N", sort=[m.strftime("%b %y") for m in months], title=None),
            xOffset="Type:N",
            y=alt.Y("Amount:Q", title=f"Amount ({CURRENCY})"),
            color=alt.Color("Type:N", scale=alt.Scale(domain=["Income", "Expenses"]), title=None),
            tooltip=["Month", "Type", alt.Tooltip("Amount:Q", format=",.0f")],
        ),
        width="stretch",
    )

    st.subheader("Daily spending")
    days = pd.date_range(start, end, freq="D")
    daily = year_df.groupby(pd.to_datetime(year_df["Date"]))["Amount"].sum().reindex(days, fill_value=0)
    heat = pd.DataFrame({
        "Date": days,
        "Week": (days - days[0]).days // 7 + 1,
        "Day": days.strftime("%a"),
        "Amount": daily.values,
    })
    st.altair_chart(
        alt.Chart(heat).mark_rect(cornerRadius=2).encode(
            x=alt.X("Week:O", title=None, axis=alt.Axis(labels=False, ticks=False)),
            y=alt.Y("Day:O", sort=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], title=None),
            color=alt.Color("Amount:Q", scale=alt.Scale(scheme="purples"), title=CURRENCY),
            tooltip=[alt.Tooltip("Date:T", format="%a %d %b %Y"), alt.Tooltip("Amount:Q", format=",.0f")],
        ).properties(height=170),
        width="stretch",
    )

    st.subheader("By category")
    cats = year_df.groupby("Category")["Amount"].agg(["sum", "size"]).sort_values("sum", ascending=False)
    st.dataframe(
        pd.DataFrame({
            "Category": cats.index.map(label),
            "Total": cats["sum"].map(money),
            "Share": (cats["sum"] / spent).map("{:.0%}".format) if spent else "",
            "Transactions": cats["size"],
            "Monthly average": (cats["sum"] / months_so_far).map(money),
        }),
        hide_index=True,
        width="stretch",
    )

    st.subheader("🧾 Tax summary")
    if not financial:
        st.caption("Switch to **Financial year** to see a tax summary.")
        return
    saved_choice = settings["tax_categories"]
    default = [c for c in (saved_choice if saved_choice is not None else [c for c in CATEGORIES if tax_hint(c)])
               if c in CATEGORIES]
    chosen = st.multiselect(
        "Tax-relevant categories", CATEGORIES, default=default, format_func=label,
        key="tax_categories", on_change=save_tax_categories,
    )
    st.caption(
        "Totals that may help when filing your return. This isn't tax advice: what you can claim "
        "depends on your tax regime and documents, so check with a tax professional."
    )
    tax_rows = year_df[year_df["Category"].isin(chosen)]
    if tax_rows.empty:
        st.info("No spending in these categories this financial year.")
        return
    totals = tax_rows.groupby("Category")["Amount"].agg(["sum", "size"])
    st.dataframe(
        pd.DataFrame({
            "Category": totals.index.map(label),
            "Total": totals["sum"].map(money),
            "Transactions": totals["size"],
            "Might count under": [tax_hint(c) or "–" for c in totals.index],
        }),
        hide_index=True,
        width="stretch",
    )
    st.download_button(
        "⬇️ Download these transactions (CSV)",
        data=tax_rows.sort_values("Date")[COLUMNS].to_csv(index=False).encode("utf-8"),
        file_name=f"tax-fy-{year}-{str(year + 1)[-2:]}.csv",
        mime="text/csv",
    )


# ---------- Goals tab ----------
def months_until(deadline: date) -> int:
    """Whole months left to save, counting the current one."""
    today = date.today()
    return max((deadline.year - today.year) * 12 + deadline.month - today.month + 1, 1)


def render_goals() -> None:
    st.caption("Save towards something specific. Record money as you put it aside, and see what's left to do.")
    saved_by_goal = contributions.groupby("Goal")["Amount"].sum()

    for goal in settings["goals"]:
        saved = saved_by_goal.get(goal["name"], 0.0)
        target = goal["target"]
        left = max(target - saved, 0)
        with st.container(border=True):
            st.markdown(f"**{goal['icon']} {goal['name']}** · {money(saved)} of {money(target)}")
            st.progress(min(max(saved / target, 0), 1.0) if target else 0.0)
            if not target:  # only from old or hand-edited data: the table needs a target of at least 1
                st.markdown("Set a target for this goal in the table below.")
            elif left == 0:
                st.markdown("🎉 Goal reached!")
            elif goal.get("deadline"):
                deadline = date.fromisoformat(goal["deadline"])
                if deadline < date.today():
                    st.markdown(f":red[Deadline passed ({deadline:%d %b %Y})] · {money(left)} to go")
                else:
                    months = months_until(deadline)
                    st.markdown(
                        f"{money(left)} to go · save **{money(left / months)}/month** "
                        f"for {months} month{'s' if months != 1 else ''} to reach it by {deadline:%d %b %Y}"
                    )
            else:
                st.markdown(f"{money(left)} to go")

    if settings["goals"]:
        st.subheader("Add money")
        with st.form("contribute", clear_on_submit=True):
            c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 3])
            goal_name = c1.selectbox("Goal", [g["name"] for g in settings["goals"]])
            direction = c2.radio("Type", ["Add", "Withdraw"], horizontal=True)
            amount = c3.number_input(f"Amount ({CURRENCY})", min_value=0.0, step=500.0, value=None, key="goal_amount")
            when = c4.date_input("Date", value=date.today(), format="DD/MM/YYYY", key="goal_date")
            note = c5.text_input("Note", placeholder="optional", key="goal_note")
            if st.form_submit_button("Save", type="primary"):
                if not amount:
                    st.error("Enter an amount greater than 0.")
                elif direction == "Withdraw" and amount > saved_by_goal.get(goal_name, 0.0) + 0.005:
                    st.error(f"You can withdraw at most {money(max(saved_by_goal.get(goal_name, 0.0), 0))} from {goal_name}.")
                else:
                    signed = amount if direction == "Add" else -amount
                    store.add("contributions", {"Date": when, "Goal": goal_name, "Amount": signed, "Note": note})
                    flash(f"{'Added' if signed > 0 else 'Withdrew'} {money(amount)} {'to' if signed > 0 else 'from'} {goal_name}")

    st.subheader("Your goals")
    st.caption("Removing a goal also removes the money recorded against it.")
    original = pd.DataFrame(settings["goals"], columns=["icon", "name", "target", "deadline"])
    original["deadline"] = pd.to_datetime(original["deadline"]).dt.date
    # Each goal's saved name, hidden: rows are matched by it, since an added row can reuse a deleted row's index
    original["was"] = original["name"]
    edited = st.data_editor(
        original,
        num_rows="dynamic",
        hide_index=True,
        column_config={
            "was": None,
            "icon": st.column_config.TextColumn("Icon", width="small", max_chars=2, default="🎯"),
            "name": st.column_config.TextColumn("Goal", required=True),
            "target": st.column_config.NumberColumn(f"Target ({CURRENCY})", min_value=1, format="%.0f", required=True),
            "deadline": st.column_config.DateColumn("Deadline (optional)", format="DD/MM/YYYY"),
        },
        key="goal_editor",
    )
    if not st.button("💾 Save goals", type="primary"):
        return

    edited["name"] = edited["name"].fillna("").astype(str).str.strip()
    edited = edited[edited["name"] != ""]
    if len(set(edited["name"])) != len(edited):
        st.error("Goal names must be unique.")
        return
    if edited["target"].isna().any():
        st.error("Every goal needs a target amount.")
        return
    # Money stays with the goal it was saved for: removed goals lose theirs, renamed ones take it along
    still_there = edited.dropna(subset=["was"])
    renames = dict(zip(still_there["was"], still_there["name"]))
    kept = contributions[contributions["Goal"].isin(renames)]
    store.save("contributions", kept.assign(Goal=kept["Goal"].map(renames)))
    settings["goals"] = [
        {
            "icon": (r["icon"] or "🎯") if pd.notna(r["icon"]) else "🎯",
            "name": r["name"],
            "target": float(r["target"]),
            "deadline": pd.Timestamp(r["deadline"]).date().isoformat() if pd.notna(r["deadline"]) else None,
        }
        for _, r in edited.iterrows()
    ]
    save_settings(settings)
    flash("Goals saved")


# ---------- Splits tab ----------
def split_people(text: str) -> list[str]:
    """'rahul, Priya ,,' -> ['Rahul', 'Priya'] (no duplicates, 'me' left out)"""
    names = [n.strip().title() for n in text.split(",")]
    return list(dict.fromkeys(n for n in names if n and n.lower() not in ("me", "i", "myself")))


def render_splits() -> None:
    st.caption(
        "Share a bill with friends. Only your share counts as your expense; "
        "the rest is tracked here until you settle up."
    )
    with st.form("split", clear_on_submit=True):
        c1, c2, c3 = st.columns([2, 2, 2])
        total = c1.number_input(f"Total bill ({CURRENCY})", min_value=0.0, step=100.0, value=None, key="split_total")
        category = c2.selectbox("Category", CATEGORIES, format_func=label, key="split_category")
        when = c3.date_input("Date", value=date.today(), format="DD/MM/YYYY", key="split_date")
        c4, c5, c6 = st.columns([3, 2, 3])
        people_text = c4.text_input("Shared with", placeholder="e.g. Rahul, Priya", key="split_people")
        payer_text = c5.text_input("Paid by", value="me", key="split_payer", help="'me', or the name of the friend who paid")
        note = c6.text_input("What was it?", placeholder="e.g. Dinner at Toit", key="split_note")
        submitted = st.form_submit_button("Split it", type="primary", icon=":material/call_split:")

    if submitted:
        people = split_people(people_text)
        paid_by_me = payer_text.strip().lower() in ("", "me", "i", "myself")
        payer = payer_text.strip().title()
        if not total:
            st.error("Enter the total bill.")
        elif not people:
            st.error("Add at least one person to split with.")
        elif not paid_by_me and payer not in people:
            st.error(f"{payer} isn't in the 'Shared with' list.")
        else:
            share = round(total / (len(people) + 1), 2)
            mine = round(total - share * len(people), 2)  # the leftover paisa is yours, so the shares add up
            text = note or "Split bill"
            expense_id = store.add("expenses", {
                "Date": when, "Category": category, "Amount": mine,
                "Payment": "Other",
                "Note": f"{text} (your share of {money(total)})", "Account": "",
            })
            # You paid: everyone else owes you their share. A friend paid: you owe them yours.
            owed = [(p, share) for p in people] if paid_by_me else [(payer, -mine)]
            for person, amount in owed:
                store.add("splits", {
                    "Date": when, "Person": person, "Amount": amount, "Note": text,
                    "Settled": 0, "ExpenseId": expense_id,
                })
            flash(f"Split {money(total)} {len(people) + 1} ways: your share is {money(mine)}")

    open_splits = splits[~splits["Settled"]]
    if splits.empty:
        st.info("No shared bills yet.")
        return

    st.subheader("Balances")
    balances_by_person = open_splits.groupby("Person")["Amount"].sum().round(2)
    # Open bills that cancel out (they owe you ₹200, you owe them ₹200) have nothing left to settle
    if evened := sorted(balances_by_person.index[balances_by_person == 0]):
        store.save("splits", splits.assign(
            Settled=(splits["Settled"] | splits["Person"].isin(evened)).astype(int)
        ))
        flash(f"Settled up with {', '.join(evened)}: your shared bills cancel out")
    balances_by_person = balances_by_person[balances_by_person != 0]
    if balances_by_person.empty:
        st.success("All settled up 🎉")
    for person, amount in balances_by_person.items():
        c1, c2 = st.columns([4, 1], vertical_alignment="center")
        c1.markdown(
            f"**{person}** owes you **{money(amount)}**" if amount > 0
            else f"You owe **{person}** **{money(-amount)}**"
        )
        if c2.button("Settle up", key=f"settle_{person}", width="stretch"):
            store.save("splits", splits.assign(
                Settled=splits["Settled"] | (splits["Person"] == person)
            ).assign(Settled=lambda s: s["Settled"].astype(int)))
            flash(f"Settled up with {person}")

    st.subheader("All shared bills")
    edited = st.data_editor(
        splits.reset_index(drop=True),
        hide_index=True,
        width="stretch",
        disabled=["Date", "Person", "Amount", "Note"],
        column_config={
            "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
            "Amount": st.column_config.NumberColumn(
                f"Owed to you ({CURRENCY})", format="%.2f", help="Negative means you owe them."
            ),
            "Settled": st.column_config.CheckboxColumn("Settled"),
            "id": None,
            "ExpenseId": None,
        },
        key="split_editor",
    )
    if st.button("💾 Save shared bills"):
        store.save("splits", edited.assign(Settled=edited["Settled"].astype(int)))
        flash("Shared bills saved")


# ---------- Accounts tab ----------
def balances(df: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for acc in settings["accounts"]:
        name = acc["name"]
        money_in = income.loc[income["Account"] == name, "Amount"].sum()
        money_out = df.loc[df["Account"] == name, "Amount"].sum()
        moved_in = transfers.loc[transfers["To"] == name, "Amount"].sum()
        moved_out = transfers.loc[transfers["From"] == name, "Amount"].sum()
        balance = acc["opening"] + money_in - money_out + moved_in - moved_out
        rows.append({"Account": name, "Type": acc["type"], "Balance": balance})
    return pd.DataFrame(rows, columns=["Account", "Type", "Balance"])


def signed_money(x: float) -> str:
    return money(x) if x >= 0 else f"-{money(-x)}"


# ---------- Cash-flow forecast ----------
FORECAST_SPENDING_DAYS = 90  # Everyday spending is averaged over this many past days
FORECAST_CHOICES = [30, 60, 90]


def main_account() -> str:
    """Where the forecast puts income, bills and spending that have no account."""
    if settings["main_account"] in ACCOUNTS:
        return settings["main_account"]
    banks = [a["name"] for a in settings["accounts"] if a["type"] == "Bank"]
    return (banks or ACCOUNTS)[0]


def _monthly_after(last: date, end: date) -> list[date]:
    """The same day of the month as `last`, every month after it up to `end`."""
    month, dates = pd.Period(last, "M") + 1, []
    while (due := due_date(month, last.day)) <= end:
        dates.append(due)
        month += 1
    return dates


def cash_forecast(df: pd.DataFrame, days: int) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, float]]:
    """Each account's expected balance for the next `days` days.

    Returns (daily balances: Date/Account/Balance, expected items: Date/Account/Item/Amount,
    everyday spending per day by account).
    """
    today, main = date.today(), main_account()
    end = today + timedelta(days=days)
    items = []  # (date, account, item, signed amount)

    def add(when: date, account: str, item: str, amount: float) -> None:
        # Anything already due but not yet recorded is expected today
        items.append((max(when, today), account if account in ACCOUNTS else main, item, amount))

    for rule in settings["recurring"]:
        if rule["active"]:
            month = pd.Period(rule["next"], "M")
            while (due := due_date(month, rule["day"])) <= end and before_end(rule, month):
                add(due, rule.get("account", ""), f"🔁 {rule['name']}", -rule["amount"])
                month += 1

    # Income and transfers that came monthly carry on, at their recent average
    for (source, account), rows in income.groupby(["Source", "Account"]):
        rows = rows.sort_values("Date")
        streak = monthly_streak(rows)
        if streak >= 2 and still_monthly(rows["Date"].iloc[-1]):
            amount = rows["Amount"].tail(streak).mean()
            for when in _monthly_after(rows["Date"].iloc[-1], end):
                add(when, account, f"💵 {source}", amount)
    for (source, target), rows in transfers.groupby(["From", "To"]):
        rows = rows.sort_values("Date")
        streak = monthly_streak(rows)
        if streak >= 2 and still_monthly(rows["Date"].iloc[-1]):
            amount = rows["Amount"].tail(streak).mean()
            for when in _monthly_after(rows["Date"].iloc[-1], end):
                add(when, source, f"↔️ To {target}", -amount)
                add(when, target, f"↔️ From {source}", amount)

    # Everyday spending: the recent daily average, leaving out what the recurring rules already cover
    everyday = df[~df["Note"].fillna("").str.startswith(("🔁", "🔔"))]
    age = (pd.Timestamp(today) - pd.to_datetime(everyday["Date"])).dt.days
    everyday = everyday[age.between(0, FORECAST_SPENDING_DAYS - 1)]
    history_days = min(FORECAST_SPENDING_DAYS, int(age.max()) + 1) if not everyday.empty else 1
    per_day = (
        everyday.assign(Account=everyday["Account"].where(everyday["Account"].isin(ACCOUNTS), main))
        .groupby("Account")["Amount"].sum() / history_days
    )
    rates = {name: float(per_day.get(name, 0)) for name in ACCOUNTS}

    items = pd.DataFrame(items, columns=["Date", "Account", "Item", "Amount"]).sort_values("Date")
    start = balances(df).set_index("Account")["Balance"]
    dates = pd.date_range(today, end).date
    rows = []
    for name in ACCOUNTS:
        mine = items[items["Account"] == name]
        for n, day in enumerate(dates):
            planned = mine.loc[mine["Date"] <= day, "Amount"].sum()
            rows.append((day, name, start[name] + planned - rates[name] * n))
    daily = pd.DataFrame(rows, columns=["Date", "Account", "Balance"])
    return daily, items.reset_index(drop=True), rates


def forecast_warnings(daily: pd.DataFrame) -> list[str]:
    """A line for each bank, wallet or cash account forecast to drop below the warning level."""
    floor = settings["forecast_floor"]
    cards = {a["name"] for a in settings["accounts"] if a["type"] == "Credit card"}
    warnings = []
    for name, rows in daily.groupby("Account", sort=False):
        below = rows[rows["Balance"] < floor]
        if name not in cards and not below.empty:
            first = below.iloc[0]
            lowest = rows.loc[rows["Balance"].idxmin()]
            warnings.append(
                f"**{name}** may drop below {signed_money(floor)} around **{first['Date']:%d %b}**, "
                f"reaching {signed_money(lowest['Balance'])} by {lowest['Date']:%d %b}."
            )
    return warnings


def render_forecast(df: pd.DataFrame) -> None:
    st.subheader("🔮 Cash-flow forecast")
    c1, c2, c3 = st.columns([2, 2, 2], vertical_alignment="bottom")
    days = c1.segmented_control(
        "Look ahead", FORECAST_CHOICES, default=FORECAST_CHOICES[0], format_func=lambda d: f"{d} days",
        key="forecast_days",
    ) or FORECAST_CHOICES[0]
    main = c2.selectbox(
        "Main account", ACCOUNTS, index=ACCOUNTS.index(main_account()),
        help="Income, bills and spending with no account are counted here.",
    )
    floor = c3.number_input(
        f"Warn below ({CURRENCY})", value=int(settings["forecast_floor"]), step=1000,
        help="Shown on the Dashboard when a bank, wallet or cash balance may drop below this in the next 30 days.",
    )
    if (main, floor) != (settings["main_account"], settings["forecast_floor"]):
        settings["main_account"], settings["forecast_floor"] = main, floor
        save_settings(settings)

    daily, items, rates = cash_forecast(df, days)
    for warning in forecast_warnings(daily):
        st.warning(warning, icon="⚠️")

    end = daily["Date"].max()
    ends = daily[daily["Date"] == end].set_index("Account")["Balance"]
    starts = balances(df).set_index("Account")["Balance"]
    cols = st.columns(min(len(ACCOUNTS), 4))
    for i, name in enumerate(ACCOUNTS):
        change = ends[name] - starts[name]
        cols[i % len(cols)].metric(
            f"{name} on {end:%d %b}", signed_money(ends[name]),
            delta=f"{'+' if change >= 0 else '-'}{money(abs(change))}",
        )

    chart_data = daily.assign(Date=pd.to_datetime(daily["Date"]))
    lines = alt.Chart(chart_data).mark_line().encode(
        x=alt.X("Date:T", title=None),
        y=alt.Y("Balance:Q", title=f"Balance ({CURRENCY})"),
        color=alt.Color("Account:N", title=None),
        tooltip=[alt.Tooltip("Date:T"), "Account:N", alt.Tooltip("Balance:Q", format=",.0f")],
    )
    floor_line = alt.Chart(pd.DataFrame({"Balance": [floor]})).mark_rule(strokeDash=[4, 4], color="gray").encode(
        y="Balance:Q"
    )
    st.altair_chart(lines + floor_line, width="stretch")

    with st.expander("What the forecast counts"):
        st.markdown(
            "- **Everyday spending** at your average per day over the last "
            f"{FORECAST_SPENDING_DAYS} days (recurring items left out, as they're listed below): "
            + ", ".join(f"{name} {money(rate)}/day" for name, rate in rates.items())
            + "\n- **Recurring expenses** on their due dates; unpaid reminders count as due today"
            "\n- **Income and transfers** that came monthly (two or more months in a row), at their recent average"
            "\n\nOne-off income, transfers and large purchases aren't predicted."
        )
        if not items.empty:
            st.dataframe(
                items, hide_index=True, width="stretch",
                column_config={
                    "Date": st.column_config.DateColumn("Date", format="DD/MM/YYYY"),
                    "Amount": st.column_config.NumberColumn(f"Amount ({CURRENCY})", format="%.0f"),
                },
            )


def render_accounts(df: pd.DataFrame) -> None:
    st.caption(
        "Track where your money is. Pick an account when adding expenses or income, and record "
        "transfers between accounts (like paying a credit card bill), which don't count as spending."
    )
    if ACCOUNTS:
        table = balances(df)
        st.metric("Total in accounts", signed_money(table["Balance"].sum()))
        cols = st.columns(min(len(table), 4))
        for i, row in table.iterrows():
            cols[i % len(cols)].metric(f"{row['Account']} · {row['Type']}", signed_money(row["Balance"]))
        unassigned = (df["Account"] == "").sum()
        if unassigned:
            st.caption(f"{unassigned} expenses have no account, so they aren't in these balances.")
    if ACCOUNTS or settings["goals"] or settings["loans"]:
        render_net_worth(df)
    if ACCOUNTS:
        render_forecast(df)

    if len(ACCOUNTS) >= 2:
        st.subheader("Transfer money")
        with st.form("transfer", clear_on_submit=True):
            c1, c2, c3, c4, c5 = st.columns([2, 2, 2, 2, 3])
            source = c1.selectbox("From", ACCOUNTS)
            target = c2.selectbox("To", ACCOUNTS, index=1)
            amount = c3.number_input(f"Amount ({CURRENCY})", min_value=0.0, step=500.0, value=None, key="transfer_amount")
            when = c4.date_input("Date", value=date.today(), format="DD/MM/YYYY", key="transfer_date")
            note = c5.text_input("Note", placeholder="e.g. card bill", key="transfer_note")
            if st.form_submit_button("Record transfer", icon=":material/swap_horiz:"):
                if source == target:
                    st.error("Pick two different accounts.")
                elif not amount:
                    st.error("Enter a transfer amount greater than 0.")
                else:
                    row = pd.DataFrame([[when, source, target, amount, note]], columns=TRANSFER_COLUMNS)
                    store.save("transfers", pd.concat([transfers, row], ignore_index=True))
                    flash(f"Moved {money(amount)} from {source} to {target}")

    if not transfers.empty:
        st.subheader("Transfers")
        edited = st.data_editor(
            transfers.reset_index(drop=True),
            num_rows="dynamic",
            hide_index=True,
            width="stretch",
            column_config={
                "Date": st.column_config.DateColumn("Date", required=True, format="DD/MM/YYYY"),
                "From": st.column_config.SelectboxColumn("From", options=ACCOUNTS, required=True),
                "To": st.column_config.SelectboxColumn("To", options=ACCOUNTS, required=True),
                "Amount": st.column_config.NumberColumn(f"Amount ({CURRENCY})", min_value=0, format="%.2f", required=True),
                "id": None,
            },
            key="transfer_editor",
        )
        if st.button("💾 Save transfers"):
            store.save("transfers", edited)
            flash("Transfers saved")

    st.subheader("Your accounts")
    original = pd.DataFrame(settings["accounts"], columns=["name", "type", "opening"])
    # Each account's saved name, hidden: rows are matched by it, since an added row can reuse a deleted row's index
    original["was"] = original["name"]
    edited = st.data_editor(
        original,
        num_rows="dynamic",
        hide_index=True,
        column_config={
            "was": None,
            "name": st.column_config.TextColumn("Name", required=True),
            "type": st.column_config.SelectboxColumn("Type", options=ACCOUNT_TYPES, default="Bank", required=True),
            "opening": st.column_config.NumberColumn(
                f"Opening balance ({CURRENCY})", format="%.2f", default=0,
                help="The balance before the first transaction recorded here. Use a negative number for money owed on a credit card.",
            ),
        },
        key="account_editor",
    )
    if not st.button("💾 Save accounts", type="primary"):
        return

    edited["name"] = edited["name"].fillna("").astype(str).str.strip()
    edited = edited[edited["name"] != ""]
    names = list(edited["name"])
    still_there = edited.dropna(subset=["was"])
    renames = {was: name for was, name in zip(still_there["was"], still_there["name"]) if was != name}
    # A name typed again in an added row is kept; one taken over by renaming another row is still removed
    removed = set(original["name"]) - set(still_there["was"]) - set(edited.loc[edited["was"].isna(), "name"])
    used = (
        set(df["Account"]) | set(income["Account"]) | set(transfers["From"]) | set(transfers["To"])
        | {r.get("account", "") for r in settings["recurring"]}
        | {loan.get("account", "") for loan in settings["loans"]}
    )
    if len(set(names)) != len(names):
        st.error("Account names must be unique.")
    elif blocked := sorted(removed & used):
        st.error(
            "These accounts still have transactions or loans paid from them, so they can't be removed: "
            + ", ".join(blocked) + ". Rename them instead."
        )
    else:
        if renames:
            save_data(df.assign(Account=df["Account"].replace(renames)))
            save_income(income.assign(Account=income["Account"].replace(renames)))
            store.save("transfers", transfers.assign(
                From=transfers["From"].replace(renames), To=transfers["To"].replace(renames)
            ))
            for rule in settings["recurring"]:
                rule["account"] = renames.get(rule.get("account", ""), rule.get("account", ""))
            for loan in settings["loans"]:
                loan["account"] = renames.get(loan.get("account", ""), loan.get("account", ""))
        settings["main_account"] = (
            None if settings["main_account"] in removed else renames.get(settings["main_account"], settings["main_account"])
        )
        settings["accounts"] = [
            {"name": r["name"], "type": r["type"] if r["type"] in ACCOUNT_TYPES else "Bank",
             "opening": float(r["opening"]) if pd.notna(r["opening"]) else 0.0}
            for _, r in edited.iterrows()
        ]
        save_settings(settings)
        flash("Accounts saved")


# ---------- Loans tab ----------
LOAN_TYPES = {"Home": "🏠", "Car": "🚗", "Personal": "👤", "Education": "🎓", "Credit card": "💳", "Gold": "🪙",
              "Other": "📄"}
MAX_LOAN_MONTHS = 600  # 50 years; a schedule longer than this is treated as never paid off


def emi_for(principal: float, rate: float, months: int) -> float:
    """The standard EMI for a loan repaid over `months` at `rate`% a year."""
    r = rate / 1200
    if r == 0:
        return principal / months
    return principal * r * (1 + r) ** months / ((1 + r) ** months - 1)


def emi_rule_name(loan: dict) -> str:
    return f"EMI {loan['name']}"


def sync_emi_rules() -> bool:
    """Set each loan's EMI rule in Recurring to stop after the loan's last EMI. True if any changed."""
    changed = False
    for loan in settings["loans"]:
        schedule = loan_schedule(loan)
        if schedule is None:  # never paid off, so no end
            until = None
        else:
            emis = schedule.loc[schedule["EMI"] > 0, "Date"]
            # Paid off before its first EMI: end the month before
            last = pd.Period(emis.iat[-1], "M") if not emis.empty else pd.Period(loan["first_emi"], "M") - 1
            until = str(last)
        for rule in settings["recurring"]:
            if rule["name"] == emi_rule_name(loan) and rule.get("until") != until:
                rule["until"] = until
                changed = True
    return changed


def loan_schedule(loan: dict, extra_monthly: float = 0.0, lump_sum: float = 0.0) -> pd.DataFrame | None:
    """Month-by-month repayment: Date, EMI, Interest, Principal, Extra, Balance.

    Recorded prepayments come off before the EMI they fall before; one that clears the loan ends it
    on its own date, with no EMI. `extra_monthly` and `lump_sum` are what-ifs starting from the next EMI.
    None if the EMI never pays the loan off (see `never_repaid`) or it takes over MAX_LOAN_MONTHS.
    """
    r = loan["rate"] / 1200
    first, today = date.fromisoformat(loan["first_emi"]), date.today()
    prepayments = [(date.fromisoformat(p["date"]), p["amount"]) for p in loan.get("prepayments", [])]
    balance, previous, rows = float(loan["principal"]), date.min, []
    columns = ["Date", "EMI", "Interest", "Principal", "Extra", "Balance"]
    if never_repaid(loan):
        return None
    for n in range(MAX_LOAN_MONTHS):
        due = due_date(pd.Period(first, "M") + n, first.day)
        paid_early = [(when, amount) for when, amount in prepayments if previous < when <= due]
        extra = sum(amount for _, amount in paid_early)
        if due > today:
            extra += extra_monthly + (lump_sum if not any(row[0] > today for row in rows) else 0)
        extra = min(extra, balance)
        balance -= extra
        if balance < 0.5:  # prepaid in full before this EMI
            rows.append((max((when for when, _ in paid_early), default=due), 0.0, 0.0, 0.0, extra, 0.0))
            return pd.DataFrame(rows, columns=columns)
        interest = balance * r
        emi = min(loan["emi"], balance + interest)
        balance -= emi - interest
        if balance < loan["emi"] / 100:  # a rounding leftover goes on this EMI, not a tiny extra one
            emi, balance = emi + balance, 0.0
        rows.append((due, emi, interest, emi - interest, extra, balance))
        previous = due
        if balance < 0.5:
            return pd.DataFrame(rows, columns=columns)
    return None


def never_repaid(loan: dict) -> bool:
    """The EMI doesn't even cover the first month's interest (the most it ever is), so the loan only grows."""
    return loan["emi"] <= loan["principal"] * loan["rate"] / 1200


def loan_status(loan: dict, schedule: pd.DataFrame) -> dict:
    today = date.today()
    paid, ahead = schedule[schedule["Date"] <= today], schedule[schedule["Date"] > today]
    left = paid["Balance"].iat[-1] if not paid.empty else float(loan["principal"])
    # The schedule takes a prepayment off at the next EMI, but it's already paid
    since = paid["Date"].iat[-1] if not paid.empty else date.min
    left -= sum(p["amount"] for p in loan.get("prepayments", []) if since < date.fromisoformat(p["date"]) <= today)
    return {
        "left": max(left, 0.0),
        "emis_left": len(ahead),
        "payoff": schedule["Date"].iat[-1],
        "interest_left": ahead["Interest"].sum(),
    }


def duration(months: int) -> str:
    years, months = divmod(months, 12)
    return " ".join(filter(None, [f"{years} yr" if years else "", f"{months} mo" if months else ""])) or "0 mo"


def emi_category() -> str:
    """The category EMIs go under when added to Recurring."""
    return next(
        (c for c in CATEGORIES if any(w in c.lower() for w in ("loan", "emi"))),
        next((c for c in ("Bills", "Other") if c in CATEGORIES), CATEGORIES[0]),
    )


def render_loan(index: int, loan: dict) -> None:
    schedule = loan_schedule(loan)
    title = f"{LOAN_TYPES.get(loan['type'], '📄')} **{loan['name']}** · {money(loan['emi'])} a month"
    if schedule is None:
        if never_repaid(loan):
            st.error(
                f"{title}: the EMI doesn't cover the interest ({money(loan['principal'] * loan['rate'] / 1200)} "
                "a month at the start), so this loan would never be paid off. Check the amount, rate and EMI below."
            )
        else:
            st.error(
                f"{title}: at this EMI the loan would take over {MAX_LOAN_MONTHS // 12} years to pay off. "
                "Check the amount, rate and EMI below."
            )
        return
    status = loan_status(loan, schedule)
    repaid = 1 - status["left"] / loan["principal"] if loan["principal"] else 1

    with st.container(border=True):
        st.markdown(title + (f" · from {loan['account']}" if loan.get("account") else ""))
        if status["left"] < 0.5:
            st.success(f"Paid off on {status['payoff']:%d %b %Y} 🎉")
            return
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Left to repay", money(status["left"]))
        c2.metric("EMIs left", status["emis_left"], help=f"Last EMI on {status['payoff']:%d %b %Y}")
        c3.metric("Interest still to pay", money(status["interest_left"]))
        c4.metric("Paid off by", f"{status['payoff']:%b %Y}")
        st.progress(min(max(repaid, 0.0), 1.0), text=f"{repaid:.0%} of {money(loan['principal'])} repaid")

        w1, w2 = st.columns(2)
        extra = w1.number_input(
            f"What if I pay extra every month? ({CURRENCY})", min_value=0, step=1000, key=f"loan_extra_{index}"
        )
        lump = w2.number_input(
            f"What if I prepay once now? ({CURRENCY})", min_value=0, step=10000, key=f"loan_lump_{index}"
        )
        chart_rows = [schedule.assign(Plan="Current plan")]
        if extra or lump:
            faster = loan_schedule(loan, extra, lump)
            sooner = len(schedule) - len(faster)
            saved = schedule["Interest"].sum() - faster["Interest"].sum()
            st.success(
                f"You'd finish in **{faster['Date'].iat[-1]:%b %Y}** instead of {status['payoff']:%b %Y}: "
                f"**{duration(sooner)} sooner**, saving **{money(saved)}** in interest."
            )
            chart_rows.append(faster.assign(Plan="With extra payments"))
        chart_data = pd.concat(chart_rows).assign(Date=lambda d: pd.to_datetime(d["Date"]))
        chart = alt.Chart(chart_data).mark_line().encode(
            x=alt.X("Date:T", title=None),
            y=alt.Y("Balance:Q", title=f"Left to repay ({CURRENCY})"),
            color=alt.Color("Plan:N", title=None, legend=alt.Legend(orient="top")),
            tooltip=[alt.Tooltip("Date:T"), "Plan:N", alt.Tooltip("Balance:Q", format=",.0f")],
        ).properties(height=220)
        st.altair_chart(chart, width="stretch")

        b1, b2 = st.columns(2)
        with b1.popover("Record a prepayment", icon=":material/payments:", width="stretch"):
            with st.form(f"prepay_{index}", clear_on_submit=True):
                when = st.date_input("Date", value="today", max_value=date.today(), format="DD/MM/YYYY")
                amount = st.number_input(f"Amount ({CURRENCY})", min_value=0.0, step=10000.0, value=None)
                if st.form_submit_button("Save prepayment", type="primary"):
                    if not amount:
                        st.error("Enter an amount greater than 0.")
                    else:
                        loan.setdefault("prepayments", []).append({"date": when.isoformat(), "amount": amount})
                        sync_emi_rules()
                        save_settings(settings)
                        flash(f"Prepayment of {money(amount)} saved for {loan['name']}")
            for p in loan.get("prepayments", []):
                st.caption(f"{date.fromisoformat(p['date']):%d %b %Y} · {money(p['amount'])}")
        if any(r["name"] == emi_rule_name(loan) for r in settings["recurring"]):
            b2.caption("✅ This EMI is in Recurring, so it's added each month and counted in the forecast.")
        elif b2.button("Add EMI to Recurring", key=f"loan_rule_{index}", icon=":material/add:", width="stretch",
                       help="Adds the EMI as an expense each month and counts it in the cash-flow forecast."):
            first = date.fromisoformat(loan["first_emi"])
            settings["recurring"] = [*settings["recurring"], {
                "name": emi_rule_name(loan), "category": emi_category(), "amount": float(loan["emi"]),
                "payment": "Net banking", "account": loan.get("account", ""), "mode": "auto",
                "day": first.day, "active": True, "next": str(max(pd.Period(first, "M"), pd.Period(first_due_month(first.day), "M"))),
            }]
            sync_emi_rules()
            save_settings(settings)
            flash(f"{emi_rule_name(loan)} added to Recurring")


def render_loans() -> None:
    st.caption(
        "Track loans and card debt: what's left, when it ends, and how much paying extra would save. "
        "Interest is worked out monthly like a standard EMI, so it can differ from your bank's figures by a little."
    )
    loans = settings["loans"]
    if sync_emi_rules():  # EMI rules made before they had an end month
        save_settings(settings)
    statuses = [(loan, loan_schedule(loan)) for loan in loans]
    active = [(loan, loan_status(loan, s)) for loan, s in statuses if s is not None]
    active = [(loan, s) for loan, s in active if s["left"] >= 0.5]
    if active:
        total_emi = sum(loan["emi"] for loan, _ in active)
        monthly_income = (
            income[pd.to_datetime(income["Date"]) >= pd.Timestamp(date.today() - timedelta(days=90))]["Amount"].sum() / 3
        )
        c1, c2, c3 = st.columns(3)
        c1.metric("Total left to repay", money(sum(s["left"] for _, s in active)))
        c2.metric("EMIs per month", money(total_emi))
        if monthly_income:
            share = total_emi / monthly_income
            c3.metric(
                "EMIs vs income", f"{share:.0%}",
                help="Your EMIs as a share of your average monthly income over the last 3 months. "
                     "Lenders usually like this below about 40–50%.",
            )
    for i, loan in enumerate(loans):
        render_loan(i, loan)

    st.subheader("Your loans")
    st.caption("Fill in the EMI, or leave it empty and give the tenure to have it worked out.")
    original = pd.DataFrame(loans, columns=["name", "type", "principal", "rate", "tenure", "emi", "first_emi", "account"])
    original["first_emi"] = pd.to_datetime(original["first_emi"]).dt.date
    original["account"] = original["account"].astype(object).fillna("")
    # Each loan's saved position, hidden: rows are matched by it, since an added row can reuse a deleted row's index
    original["was"] = range(len(original))
    edited = st.data_editor(
        original,
        num_rows="dynamic",
        hide_index=True,
        width="stretch",
        column_config={
            "was": None,
            "name": st.column_config.TextColumn("Name", required=True),
            "type": st.column_config.SelectboxColumn("Type", options=list(LOAN_TYPES), default="Personal", required=True),
            "principal": st.column_config.NumberColumn(f"Borrowed ({CURRENCY})", min_value=0, format="%.0f", required=True),
            "rate": st.column_config.NumberColumn("Interest (% a year)", min_value=0, max_value=60, format="%.2f", required=True),
            "tenure": st.column_config.NumberColumn("Tenure (months)", min_value=1, max_value=MAX_LOAN_MONTHS, format="%d"),
            "emi": st.column_config.NumberColumn(f"EMI ({CURRENCY})", min_value=0, format="%.0f"),
            "first_emi": st.column_config.DateColumn("First EMI", format="DD/MM/YYYY", required=True),
            "account": account_column(),
        },
        key="loan_editor",
    )
    if not st.button("💾 Save loans", type="primary"):
        return

    saved, problems = [], []
    for row_no, (_, r) in enumerate(edited.iterrows(), start=1):
        name = str(r["name"]).strip() if pd.notna(r["name"]) else ""
        if not name and pd.isna(r["principal"]):
            continue  # blank row
        emi = r["emi"] if pd.notna(r["emi"]) and r["emi"] > 0 else None
        tenure = int(r["tenure"]) if pd.notna(r["tenure"]) else None
        if not name or pd.isna(r["principal"]) or r["principal"] <= 0 or pd.isna(r["rate"]) \
                or pd.isna(r["first_emi"]) or not (emi or tenure):
            problems.append(f"row {row_no}")
            continue
        old = loans[int(r["was"])] if pd.notna(r["was"]) else {}
        saved.append({
            "name": name,
            "type": r["type"] if r["type"] in LOAN_TYPES else "Other",
            "principal": float(r["principal"]),
            "rate": float(r["rate"]),
            "tenure": tenure,
            # A worked-out EMI is rounded up to the paisa, so it pays the loan off within the tenure
            "emi": round(float(emi), 2) if emi else math.ceil(emi_for(r["principal"], r["rate"], tenure) * 100) / 100,
            "first_emi": pd.Timestamp(r["first_emi"]).date().isoformat(),
            "account": r["account"] if r["account"] in ACCOUNTS else "",
            "prepayments": old.get("prepayments", []),
        })
        # A renamed loan keeps its EMI in Recurring
        if old and old["name"] != name:
            for rule in settings["recurring"]:
                if rule["name"] == emi_rule_name(old):
                    rule["name"] = emi_rule_name(saved[-1])

    if problems:
        st.error(
            "Each loan needs a name, amount borrowed, interest rate, first EMI date, and an EMI or tenure. Check "
            + ", ".join(problems) + "."
        )
    elif len({l["name"] for l in saved}) != len(saved):
        st.error("Loan names must be unique.")
    else:
        settings["loans"] = saved
        sync_emi_rules()
        save_settings(settings)
        flash("Loans saved")


# ---------- Net worth (Accounts tab) ----------
NET_WORTH_MONTHS = 60  # How far back the history goes


def loan_left_on(loan: dict, schedule: pd.DataFrame | None, day: date) -> float:
    """What was still owed on a loan at the end of `day`."""
    first = date.fromisoformat(loan["first_emi"])
    if day < first - timedelta(days=31):  # not taken yet: assume it was paid out a month before the first EMI
        return 0.0
    if schedule is None:  # the EMI doesn't cover the interest, so nothing is ever repaid
        return float(loan["principal"])
    paid = schedule[schedule["Date"] <= day]
    return float(paid["Balance"].iat[-1]) if not paid.empty else float(loan["principal"])


def net_worth_history(df: pd.DataFrame, with_goals: bool) -> pd.DataFrame:
    """Month-end Accounts, Goals, Other assets, Loans (as a negative) and Net worth, rebuilt from your history.

    The current month is as of today. Transfers between your accounts don't change the total,
    so only income and expenses move it.
    """
    today = date.today()
    in_accounts = df[df["Account"].isin(ACCOUNTS)]
    income_in = income[income["Account"].isin(ACCOUNTS)]
    opening = sum(a["opening"] for a in settings["accounts"])
    loans = [(loan, loan_schedule(loan)) for loan in settings["loans"]]

    starts = [*in_accounts["Date"], *income_in["Date"], *(contributions["Date"] if with_goals else [])]
    starts += [date.fromisoformat(loan["first_emi"]) - timedelta(days=31) for loan in settings["loans"]]
    assets = [(a["value"], date.fromisoformat(a["since"]) if a.get("since") else date.min) for a in settings["assets"]]
    starts += [since for _, since in assets if since != date.min]
    current = pd.Period(today, "M")
    # Today is included so a loan or asset that starts in a future month still leaves this month to show
    first = max(pd.Period(min([*starts, today]), "M"), current - (NET_WORTH_MONTHS - 1))

    rows = []
    for month in pd.period_range(first, current, freq="M"):
        day = min(month.end_time.date(), today)
        accounts = opening + income_in.loc[income_in["Date"] <= day, "Amount"].sum() \
            - in_accounts.loc[in_accounts["Date"] <= day, "Amount"].sum()
        goals = contributions.loc[contributions["Date"] <= day, "Amount"].sum() if with_goals else 0.0
        owned = sum(value for value, since in assets if since <= day)
        owed = sum(loan_left_on(loan, schedule, day) for loan, schedule in loans)
        rows.append((str(month), accounts, goals, owned, -owed, accounts + goals + owned - owed))
    return pd.DataFrame(rows, columns=["Month", "Accounts", "Goals", "Other assets", "Loans", "Net worth"])


def render_net_worth(df: pd.DataFrame) -> None:
    st.subheader("📈 Net worth")
    with_goals = st.toggle(
        "Include goal savings", value=settings["networth_goals"],
        help="Turn this off if the money you put towards goals stays in one of your accounts above, "
             "so it isn't counted twice.",
    )
    if with_goals != settings["networth_goals"]:
        settings["networth_goals"] = with_goals
        save_settings(settings)

    history = net_worth_history(df, with_goals)
    now = history.iloc[-1]
    before = history.iloc[-2] if len(history) > 1 else None
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric(
        "Net worth", signed_money(now["Net worth"]),
        delta=signed_money(now["Net worth"] - before["Net worth"]) + " vs last month" if before is not None else None,
    )
    c2.metric("In accounts", signed_money(now["Accounts"]))
    c3.metric("Goal savings", money(now["Goals"]) if with_goals else "Not counted")
    c4.metric("Other assets", money(now["Other assets"]))
    c5.metric("Loans left", money(-now["Loans"]))

    if len(history) > 1:
        parts = history.melt(id_vars="Month", value_vars=["Accounts", "Goals", "Other assets", "Loans"],
                             var_name="Part", value_name="Amount")
        bars = alt.Chart(parts).mark_bar(opacity=0.5).encode(
            x=alt.X("Month:O", title=None),
            y=alt.Y("Amount:Q", title=f"Amount ({CURRENCY})", stack="zero"),
            color=alt.Color("Part:N", title=None, scale=alt.Scale(
                domain=["Accounts", "Goals", "Other assets", "Loans"],
                range=["#4c78a8", "#54a24b", "#b279a2", "#e45756"],
            ), legend=alt.Legend(orient="top")),
            tooltip=["Month:O", "Part:N", alt.Tooltip("Amount:Q", format=",.0f")],
        )
        line = alt.Chart(history).mark_line(point=True, color="black").encode(
            x="Month:O",
            y="Net worth:Q",
            tooltip=["Month:O", alt.Tooltip("Net worth:Q", format=",.0f")],
        )
        st.altair_chart(bars + line, width="stretch")
        st.caption(
            "Bars show what you have (accounts, goal savings, other assets) and owe (loans, below zero); "
            "the line is your net worth. Expenses with no account aren't included."
        )
        with st.expander("Month by month"):
            table = history.assign(Change=history["Net worth"].diff()).iloc[::-1]
            st.dataframe(
                table, hide_index=True, width="stretch",
                column_config={
                    c: st.column_config.NumberColumn(f"{c} ({CURRENCY})", format="%.0f")
                    for c in ["Accounts", "Goals", "Other assets", "Loans", "Net worth", "Change"]
                },
            )

    with st.expander("Other assets", expanded=bool(settings["loans"]) and not settings["assets"]):
        st.caption(
            "Things you own outside your accounts, like a home, car, investments or gold, at roughly what they're "
            "worth today. Each counts from its **Owned since** month (leave empty to count it in every month). "
            "If you have a home or car loan, add the home or car here so your net worth isn't just the loan."
        )
        original = pd.DataFrame(settings["assets"], columns=["name", "value", "since"])
        original["since"] = pd.to_datetime(original["since"]).dt.date
        edited = st.data_editor(
            original,
            num_rows="dynamic",
            hide_index=True,
            column_config={
                "name": st.column_config.TextColumn("Asset", required=True),
                "value": st.column_config.NumberColumn(f"Value ({CURRENCY})", min_value=0, format="%.0f", required=True),
                "since": st.column_config.DateColumn("Owned since (optional)", format="DD/MM/YYYY"),
            },
            key="asset_editor",
        )
        if st.button("💾 Save assets"):
            edited["name"] = edited["name"].fillna("").astype(str).str.strip()
            edited = edited[edited["name"] != ""]
            if edited["value"].isna().any():
                st.error("Every asset needs a value.")
            else:
                settings["assets"] = [
                    {"name": r["name"], "value": float(r["value"]),
                     "since": pd.Timestamp(r["since"]).date().isoformat() if pd.notna(r["since"]) else None}
                    for _, r in edited.iterrows()
                ]
                save_settings(settings)
                flash("Assets saved")


# ---------- Settings tab: backups ----------
def backup_label(path: Path) -> str:
    # File names look like expenses_2026-10-30_101500_auto.db
    _, day, clock, *kind = path.stem.split("_")
    when = pd.Timestamp(f"{day} {clock[:2]}:{clock[2:4]}")
    tag = {"auto": "automatic", "manual": "manual", "before-restore": "before a restore"}.get("_".join(kind), "")
    return f"{when:%d %b %Y, %H:%M}" + (f" ({tag})" if tag else "")


def render_account() -> None:
    st.divider()
    st.subheader("🔑 Change password")
    with st.form("change_password", clear_on_submit=True):
        old = st.text_input("Current password", type="password")
        new = st.text_input("New password (at least 8 characters)", type="password")
        repeat = st.text_input("Repeat new password", type="password")
        if st.form_submit_button("Change password"):
            if new != repeat:
                st.error("The new passwords don't match.")
            elif error := users.change_password(st.session_state["user"], old, new):
                st.error(error)
            else:
                flash("Password changed")


def render_backups() -> None:
    st.divider()
    st.subheader("🗄️ Backups")
    st.caption(
        "A backup is made automatically the first time you open the app each day "
        f"(the last 14 are kept). They're in the {store.backup_dir.relative_to(APP_DIR).as_posix()} folder."
    )
    b1, b2 = st.columns(2)
    if b1.button("Back up now", width="stretch"):
        store.backup("manual")
        flash("Backup created")
    b2.download_button(
        "⬇️ Download database", data=store.db_path.read_bytes(), file_name="expenses.db",
        mime="application/vnd.sqlite3", width="stretch",
    )

    backups = store.list_backups()
    if not backups:
        return
    choice = st.selectbox("Restore from backup", backups, format_func=backup_label)
    confirm = st.checkbox(
        "Replace my current data with this backup (a copy of the current data is saved first)", key="restore_confirm"
    )
    if st.button("♻️ Restore backup", disabled=not confirm):
        store.restore(choice)
        st.session_state.pop("restore_confirm")  # each restore needs its own tick
        flash(f"Restored the backup from {backup_label(choice)}")


# ---------- Main page ----------
st.title("💰 Expense Tracker")

(tab_dash, tab_year, tab_expenses, tab_income, tab_goals, tab_splits, tab_accounts, tab_loans, tab_recurring,
 tab_import, tab_settings) = st.tabs([
    "📊 Dashboard", "📅 Year", "📋 Expenses", "💵 Income", "🎯 Goals", "🤝 Splits", "🏦 Accounts", "🏛️ Loans",
    "🔁 Recurring", "📥 Import", "⚙️ Settings",
])
with tab_dash:
    render_dashboard(df)
with tab_year:
    render_year(df)
with tab_expenses:
    render_expenses(df)
with tab_income:
    render_income(df)
with tab_goals:
    render_goals()
with tab_splits:
    render_splits()
with tab_accounts:
    render_accounts(df)
with tab_loans:
    render_loans()
with tab_import:
    render_import(df)
with tab_recurring:
    render_subscriptions(df)
    render_recurring()
with tab_settings:
    render_settings(df)
    render_learned_rules()
    render_backups()
    render_account()
