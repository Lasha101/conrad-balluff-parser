"""conrad.de parser: logs in with your own account and saves Balluff products to a CSV file.

How it works (more in README.md):
1. Opens a visible browser and logs in through the site's own login form (e-mail and password from .env).
2. Opens the BALLUFF brand page and scrolls down like a person until enough products are listed.
3. Visits the products one by one, with a random 5-10 second pause in between, and reads each page:
   - product details from the data the page embeds for its own app (<script id="__NUXT_DATA__">),
   - breadcrumbs from the page's schema.org block and buy-box texts exactly as shown on screen,
   - all seller offers from the answer to the offers request the page makes by itself.
4. Writes one CSV row per product right away, so nothing is lost if the run stops early.

Usage:  .venv/bin/python conrad_parser.py       # 100 products
        .venv/bin/python conrad_parser.py 5     # try it with 5 products first
"""
import csv
import json
import os
import random
import re
import stat
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

# Playwright's own Chromium was installed inside .venv (see README), so everything stays in this folder.
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

from bs4 import BeautifulSoup  # noqa: E402
from dotenv import dotenv_values  # noqa: E402
from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

HERE = Path(__file__).resolve().parent
ENV_FILE = HERE / ".env"
CSV_FILE = HERE / "balluff_products.csv"
START_URL = "https://www.conrad.de/"
BRAND_URL = "https://www.conrad.de/de/marken/balluff.html"
DEFAULT_PRODUCT_COUNT = 100
HUMAN_CHECK_SECONDS = 300  # how long to wait for a person to solve a Cloudflare check
# Ubuntu only lets browsers with an AppArmor profile use Chromium's security sandbox. Brave has one,
# Playwright's downloaded Chromium has not, so we prefer Brave (with a fresh, empty profile).
BRAVE = Path("/opt/brave.com/brave/brave")

COLUMNS = [
    "url", "breadcrumbs", "title", "brand", "image_url", "all_image_urls", "key_specs", "datasheet_url",
    "delivery", "stock", "seller", "price", "total_price", "price_note", "price_tiers",
    "ean", "manufacturer_part_number", "order_number", "highlights", "technical_data", "documents",
    "description", "notes", "offers_count", "offers", "scraped_at",
]
VAT_NOTE = {"GROSS": "inkl. MwSt.", "NET": "zzgl. MwSt."}


class SiteBlocked(Exception):
    """conrad.de pushes back (a check nobody solved, "too many requests"). We stop instead of retrying."""


# ---------------------------------------------------------------- behaving like a person

def pause(page, low=5.0, high=10.0):
    page.wait_for_timeout(random.uniform(low, high) * 1000)


def click(page, locator):
    pause(page, 0.8, 2.0)  # people look before they click
    locator.click()


def type_like_a_person(page, locator, text):
    click(page, locator)
    locator.press_sequentially(text, delay=random.randint(80, 160))


def scroll_like_a_person(page, steps):
    """Mostly down, now and then a little back up, with short reading pauses."""
    for _ in range(steps):
        distance = random.randint(350, 800) if random.random() < 0.75 else -random.randint(150, 400)
        page.mouse.wheel(0, distance)
        pause(page, 0.6, 1.6)


def page_title(page):
    try:
        return page.title()
    except Exception:  # the page is in the middle of loading
        return ""


def wait_for_human_check(page):
    """Cloudflare sometimes shows "Nur einen Moment..." first. It usually passes by itself; if it shows
    a checkbox, the person at the screen clicks it. We wait for that instead of reloading again and again."""
    waited = 0
    while "moment" in page_title(page).lower():
        if waited == 0:
            print("  Cloudflare is checking the browser. If a checkbox appears in the window, please click it.")
        if waited >= HUMAN_CHECK_SECONDS:
            raise SiteBlocked("the Cloudflare check did not pass")
        page.wait_for_timeout(2000)
        waited += 2


def open_page(page, url, referer=None):
    """Load a page like typing its address (or following a link from `referer`)."""
    response = page.goto(url, referer=referer, wait_until="domcontentloaded", timeout=90_000)
    if response is not None and response.status == 429:
        raise SiteBlocked("conrad.de answered 'too many requests'")
    wait_for_human_check(page)
    page.wait_for_load_state("load", timeout=90_000)


# ---------------------------------------------------------------- start page and login

def button(page, text):
    """The visible button with this text. (A plain text lookup still works while a dialog marks the
    rest of the page as hidden for screen readers, which role-based lookups would respect.)"""
    return page.locator("button", has_text=text).filter(visible=True).first


def answer_start_page_dialogs(page):
    """On a first visit a cookie dialog and a "business or private customer?" choice cover the page.
    Brave hides the cookie dialog by itself; then there is nothing to answer."""
    try:  # the cookie dialog appears a few seconds after the page has loaded
        button(page, "Annehmen").wait_for(timeout=15_000)
    except PlaywrightTimeout:
        pass
    else:
        only_necessary = page.get_by_text(re.compile(r"nur notwendige", re.I)).filter(visible=True)
        click(page, only_necessary.first if only_necessary.count() else button(page, "Annehmen"))
    business = button(page, "Ich bin Geschäftskunde")
    try:  # after login the account type decides the view anyway
        business.wait_for(timeout=10_000)
        click(page, business)
    except PlaywrightTimeout:
        pass


def log_in(page, email, password):
    """Log in through the site's own form: Konto -> Anmelden -> e-mail -> Weiter -> password -> Login."""
    click(page, page.locator('[data-e2e="header-accountButton"]'))
    click(page, button(page, re.compile(r"^\s*Anmelden\s*$")))
    page.wait_for_url(lambda url: urlsplit(url).hostname == "accounts.conrad.com", timeout=60_000)
    wait_for_human_check(page)
    type_like_a_person(page, page.locator('input[name="username"]'), email)
    click(page, button(page, "Weiter"))
    type_like_a_person(page, page.locator('input[type="password"]'), password)
    click(page, button(page, "Login"))
    try:  # the login URL contains "returnTo=https://www.conrad.de/", so compare the host, not the whole URL
        page.wait_for_url(lambda url: urlsplit(url).hostname == "www.conrad.de", timeout=90_000)
    except PlaywrightTimeout:
        sys.exit("Login failed: conrad.de did not accept the e-mail and password from .env.")
    wait_for_human_check(page)
    if "userStatus=logged-in" not in page.url:  # where conrad.de sends you after a successful login
        sys.exit("Login failed: conrad.de did not confirm the login.")


# ---------------------------------------------------------------- product list

PRODUCT_LINKS = 'a[href*="/de/p/balluff-"]'
NEXT_PAGE = 'nav.pagination [aria-label="nächste"]:not(.disabled)'  # greyed out on the last page


def collect_product_urls(page, count):
    """Go through the BALLUFF brand list like a person: scroll down a page (90 products) to the page
    navigation, click "nächste" (next), and so on, until `count` products are listed."""
    open_page(page, BRAND_URL)
    urls = []
    while True:
        links = page.locator(PRODUCT_LINKS)
        links.first.wait_for(timeout=60_000)
        found = links.evaluate_all("links => links.map(link => link.href.split(/[?#]/)[0])")
        urls += [url for url in dict.fromkeys(found)  # keep the list order, drop duplicates
                 if url.startswith("https://www.conrad.de/de/p/") and url not in urls]
        next_page = page.locator(NEXT_PAGE)
        if len(urls) >= count or next_page.count() == 0:
            return urls[:count]
        scroll_down_to(page, page.locator("nav.pagination"))
        first_product = links.first.get_attribute("href")
        click(page, next_page.first)
        for _ in range(60):  # wait (max. 30 s) until the next page's products have replaced the old ones
            page.wait_for_timeout(500)
            if links.first.get_attribute("href") != first_product:
                break
        wait_for_human_check(page)
        page.keyboard.press("Home")  # back to the top of the new page
        pause(page, 2, 4)


def scroll_down_to(page, locator, max_steps=80):
    """Scroll with the mouse wheel, like a person, until the element is on screen."""
    window_height = page.evaluate("window.innerHeight")
    for _ in range(max_steps):
        box = locator.bounding_box()
        if box and 0 <= box["y"] <= window_height - box["height"]:
            return
        scroll_like_a_person(page, steps=1)


# ---------------------------------------------------------------- product page

offers_by_product = {}  # product number -> offers; filled from the page's own offers request


def remember_offers(response):
    """Each product page asks for its offers itself (callOrigin=requestOffers); keep a copy of the answer."""
    if "/api/product/offers" not in response.url or "callOrigin=requestOffers" not in response.url:
        return
    try:
        for product in response.json().get("products", []):
            offers_by_product[product["productId"].lstrip("0")] = product.get("offers") or []
    except Exception:
        pass  # an unreadable answer just means "no offers"


def wait_for_offers(page, product_number, seconds=20):
    for _ in range(seconds * 2):
        if product_number in offers_by_product:
            return offers_by_product.pop(product_number)
        page.wait_for_timeout(500)
    return []


def read_product(page, url):
    open_page(page, url, referer=BRAND_URL)
    product_number = re.search(r"-(\d+)\.html$", url).group(1)
    offers = wait_for_offers(page, product_number)
    scroll_like_a_person(page, steps=random.randint(3, 6))  # look at the page like a reader would
    soup = BeautifulSoup(page.content(), "html.parser")
    product = embedded_product(soup, product_number)
    full = product.get("productFullInformation") or {}
    short = product.get("productShortInformation") or {}
    manuals = (product.get("productMedia") or {}).get("manuals") or []
    seller = seller_line(soup)
    return {
        "url": url,
        "breadcrumbs": breadcrumbs(soup),
        "title": short.get("title") or "",
        "brand": (product.get("brand") or {}).get("displayName") or "",
        "image_url": (short.get("mainImage") or {}).get("imageUrl") or "",
        "all_image_urls": " | ".join(image.get("imageUrl") or "" for image in product.get("productImages") or []),
        "key_specs": key_specs(soup),
        "datasheet_url": datasheet_url(manuals),
        "delivery": one_line(buybox_text(page, "fast-track-delivery")),
        "stock": one_line(buybox_text(page, "current-offer-availability")),
        "seller": seller,
        "price": one_line(buybox_text(page, "product-price")),
        "total_price": one_line(buybox_text(page, "total-price-label")),
        "price_note": one_line(buybox_text(page, "product-price-info")),
        "price_tiers": price_tiers(buybox_offer(offers, seller)),
        "ean": full.get("eanCode") or "",
        "manufacturer_part_number": (full.get("manufacturer") or {}).get("id") or "",
        "order_number": order_number(soup),
        "highlights": highlights(full),
        "technical_data": technical_data(full),
        "documents": documents(manuals),
        "description": BeautifulSoup(full.get("description") or "", "html.parser").get_text(" ", strip=True),
        "notes": notes(soup),
        "offers_count": len(offers),
        "offers": " | ".join(describe_offer(offer) for offer in offers),
        "scraped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


# ---------------------------------------------------------------- reading the page

def decode_nuxt_data(raw_json):
    """Nuxt stores the page data as one flat JSON list ("devalue" format): containers hold list positions
    instead of values, and some values are tagged, e.g. ["Reactive", 7]. This rebuilds the real objects."""
    table = json.loads(raw_json)
    built = {}

    def build(position):
        if not isinstance(position, int) or position < 0:  # negative numbers stand for undefined, NaN, ...
            return None
        if position in built:
            return built[position]
        item = table[position]
        if isinstance(item, list) and item and isinstance(item[0], str):  # a tagged value
            tag, rest = item[0], item[1:]
            if tag in ("Date", "RegExp", "BigInt"):
                built[position] = rest[0] if rest else None
            elif tag == "Set":
                built[position] = [build(p) for p in rest]
            elif tag in ("Map", "null"):  # key/value pairs ("null" objects keep their keys as plain text)
                pairs = zip(rest[::2], rest[1::2])
                built[position] = {(build(k) if tag == "Map" else k): build(v) for k, v in pairs}
            else:  # Reactive, Ref, ShallowReactive, ... just wrap one value
                built[position] = build(rest[0]) if rest else None
        elif isinstance(item, list):
            built[position] = []  # register first: the data may point back to itself
            built[position].extend(build(p) for p in item)
        elif isinstance(item, dict):
            built[position] = {}
            built[position].update((key, build(p)) for key, p in item.items())
        else:
            built[position] = item
        return built[position]

    return build(0)


def embedded_product(soup, product_number):
    """The product object the page embeds for its own app (the one with "productFullInformation")."""
    script = soup.find("script", id="__NUXT_DATA__")
    if script is None or not script.string:
        raise ValueError("the page has no embedded product data")
    found, seen, todo = [], set(), [decode_nuxt_data(script.string)]
    while todo:  # walk through the whole data tree
        node = todo.pop()
        if id(node) in seen:
            continue
        seen.add(id(node))
        if isinstance(node, dict):
            if "productFullInformation" in node:
                found.append(node)
            todo.extend(node.values())
        elif isinstance(node, list):
            todo.extend(node)
    for product in found:
        if str(product.get("shortProductNumber")) == product_number:
            return product
    if found:
        return found[0]
    raise ValueError("no product data found on the page")


def breadcrumbs(soup):
    script = soup.find("script", attrs={"data-hid": "pdp-breadcrumb-ld"})
    if script is None or not script.string:
        return ""
    items = json.loads(script.string).get("itemListElement") or []
    return " > ".join(item.get("name", "") for item in sorted(items, key=lambda item: item.get("position", 0)))


def key_specs(soup):
    """The short table next to the title, e.g. "Produkt-Art: Induktiver Sensor | Ausgänge: PNP"."""
    specs = []
    for row in soup.select(".productImageAndData__dataTechnical div.justify-between"):
        cells = [cell.get_text(" ", strip=True) for cell in row.find_all(recursive=False)]
        if len(cells) == 2 and cells[1] != "Download":  # the last row is the data-sheet button
            specs.append(f"{cells[0]}: {cells[1]}")
    return " | ".join(specs)


def notes(soup):
    """The "Hinweise" box, e.g. "Auftragsbezogene Fertigung ..." (the page repeats it for mobile)."""
    texts = [item.get_text(" ", strip=True) for item in soup.select("li.shopObjectAdvisory__listItem")]
    return " | ".join(dict.fromkeys(texts))


def seller_line(soup):
    """"Verkauf und Versand durch Conrad Electronic" (business view: "Verkauf durch ... / Versand durch ...")."""
    lines = [one_line(span.get_text(" ")) for span in soup.select('[data-e2e="product-seller-info"] span')]
    return " / ".join(dict.fromkeys(line for line in lines if re.match(r"(Verkauf|Versand)\b.* durch ", line)))


def order_number(soup):
    """Conrad shows it as "Bestell-Nr.: 3416652 - 62"."""
    match = re.search(r"Bestell-Nr\.:\s*(\d+(?:\s*-\s*\d+)?)", soup.get_text(" ", strip=True))
    return match.group(1) if match else ""


def buybox_text(page, hook):
    """Visible text of the buy-box element marked data-e2e="<hook>" ("" if this page has none)."""
    element = page.locator(f'[data-e2e="{hook}"]').first
    return element.inner_text() if element.count() else ""


def buybox_offer(offers, seller):
    """The offer the buy box shows: the one whose seller ships it ("... Versand durch Elektro4000.de")."""
    shipper = re.search(r"Versand durch (.+)", seller)
    for offer in offers:
        if shipper and offer.get("sellerName") == shipper.group(1).strip():
            return offer
    return offers[0] if offers else None


# ---------------------------------------------------------------- formatting

def one_line(text):
    return " ".join(text.split())


def money(value):
    """32.99 -> "32,99 €", written the way the site writes prices."""
    return f"{value:.2f} €".replace(".", ",") if isinstance(value, (int, float)) else ""


def highlights(full):
    """"Highlights & Details": the headline and its bullet points."""
    texts = [full.get("headline") or ""] + (full.get("features") or [])
    return " | ".join(one_line(text) for text in texts if text)


def technical_data(full):
    """The whole "Technische Daten" table: "Produkt-Art: Induktiver Sensor | Schaltfrequenz: 2500 Hz | ..."."""
    attributes = full.get("technicalAttributes") or []
    return " | ".join(f"{attribute.get('attributeName')}: {attribute_value(attribute)}" for attribute in attributes)


def attribute_value(attribute):
    """"3.2 mm", or several values joined: "CE, UKCA"."""
    values = []
    for value in attribute.get("values") or []:
        unit = (value.get("unit") or {}).get("name") or ""
        values.append(f"{value.get('value', '')} {unit}".strip())
    return ", ".join(values)


def documents(manuals):
    """"Dokumente & Downloads": "Datenblatt (de): https://... | Bedienungs- und Sicherheitshinweise (en): ..."."""
    return " | ".join(f"{manual.get('typeName')} ({manual.get('language')}): {manual['fullUrl']}"
                      for manual in manuals if manual.get("fullUrl"))


def datasheet_url(manuals):
    sheets = [manual for manual in manuals if manual.get("typeName") == "Datenblatt" and manual.get("fullUrl")]
    german = [manual for manual in sheets if manual.get("language") == "de"]
    return (german or sheets)[0]["fullUrl"] if sheets else ""


def price_tiers(offer):
    """The quantity-price table of the buy box: "1 Stück: 24,81 € | 5 Stück: 22,44 € (9 % = 11,60 € gespart)"."""
    tiers = []
    for tier in ((offer or {}).get("price") or {}).get("priceScales") or []:
        text = f"{tier.get('scaleFrom')} Stück: {money(tier.get('pricePerUnit'))}"
        if tier.get("savedAmount"):
            text += f" ({tier.get('savedPercentage')} % = {money(tier.get('savedAmount'))} gespart)"
        tiers.append(text)
    return " | ".join(tiers)


def describe_offer(offer):
    """One line of the "Alle Angebote" list, e.g.
    "venforce GmbH: 21,74 € zzgl. MwSt., 66 Stück, versandkostenfrei, Lieferung 2026-11-02, Min. 1, Vielfacher 1"."""
    price = offer.get("price") or {}
    availability = offer.get("availability") or {}
    parts = [
        f"{money(price.get('unitPrice'))} {VAT_NOTE.get(price.get('taxSchema'), '')}".strip(),
        f"{availability['stockQuantity']} Stück" if availability.get("stockQuantity") else "",  # 0: not shown
        "versandkostenfrei" if price.get("freeShipping") else f"Versand {money(price.get('minShippingCosts'))}",
        f"Lieferung {availability['promisedDeliveryDate']}" if availability.get("promisedDeliveryDate") else "",
        f"Min. {offer.get('minimumOrderQuantity') or 1}",
        f"Vielfacher {offer['multiplier']}" if offer.get("multiplier") else "",
    ]
    return f"{offer.get('sellerName', '?')}: " + ", ".join(part for part in parts if part)


def csv_safe(value):
    """Spreadsheets run cells that start with = + - @ as formulas. Offer texts come from third-party
    sellers, so such cells get a leading apostrophe ("CSV injection" protection)."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(("=", "+", "-", "@", "\t", "\r")) else text


# ---------------------------------------------------------------- main

def read_credentials():
    """E-mail and password come only from .env (or environment variables) and are never printed."""
    if ENV_FILE.exists() and ENV_FILE.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        print("Warning: other users on this computer can read .env. Fix it with:  chmod 600 .env")
    settings = dotenv_values(ENV_FILE, interpolate=False)  # take the password exactly as written
    email = os.environ.get("CONRAD_EMAIL") or settings.get("CONRAD_EMAIL")
    password = os.environ.get("CONRAD_PASSWORD") or settings.get("CONRAD_PASSWORD")
    if not email or not password:
        sys.exit("Please put CONRAD_EMAIL and CONRAD_PASSWORD into .env (see .env.example).")
    return email, password


def launch_browser(playwright):
    options = {
        "headless": False,  # visible on purpose: a person can solve a Cloudflare check if one appears
        # Without this switch Chromium announces "controlled by automated software", and Cloudflare
        # then checks every single page.
        "ignore_default_args": ["--enable-automation"],
        "args": ["--disable-blink-features=AutomationControlled", "--start-maximized"],
    }
    if BRAVE.exists():
        return playwright.chromium.launch(executable_path=BRAVE, chromium_sandbox=True, **options)
    print("Note: Brave not found, using Playwright's Chromium (without Chromium's sandbox on Ubuntu).")
    return playwright.chromium.launch(**options)


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else DEFAULT_PRODUCT_COUNT
    email, password = read_credentials()
    saved = 0
    with sync_playwright() as playwright:
        browser = launch_browser(playwright)
        page = browser.new_context(locale="de-DE", no_viewport=True).new_page()
        page.on("response", remember_offers)
        try:
            print("Opening conrad.de ...")
            open_page(page, START_URL)
            answer_start_page_dialogs(page)
            print("Logging in ...")
            log_in(page, email, password)
            print("Collecting product links from the BALLUFF brand page ...")
            urls = collect_product_urls(page, count * 3 // 2 + 10)  # spares for unavailable products
            print(f"Found {len(urls)} products, reading {count} of them (about 15 seconds each) ...")
            with CSV_FILE.open("w", newline="", encoding="utf-8-sig") as file:  # utf-8-sig: Excel shows umlauts
                writer = csv.DictWriter(file, fieldnames=COLUMNS)
                writer.writeheader()
                for url in urls:
                    if saved == count:
                        break
                    pause(page)  # random 5-10 s between products, like a person reading
                    try:
                        row = read_product(page, url)
                    except SiteBlocked:
                        raise
                    except Exception as error:  # one odd page should not stop the whole run
                        print(f"  skipped {url}: {type(error).__name__}: {str(error)[:150]}")
                        continue
                    if not row["price"]:  # "Nicht verfügbar": no price, no offers, so take the next product
                        print(f"  skipped (not available): {row['title'][:70]}")
                        continue
                    writer.writerow({column: csv_safe(value) for column, value in row.items()})
                    file.flush()
                    saved += 1
                    print(f"  [{saved}/{count}] {row['title'][:80]}")
        except SiteBlocked as reason:
            print(f"Stopping: {reason}. Try again later.")
        except KeyboardInterrupt:
            print("Stopped by you.")
        finally:
            browser.close()
    print(f"Saved {saved} products to {CSV_FILE.name}")


if __name__ == "__main__":
    main()
