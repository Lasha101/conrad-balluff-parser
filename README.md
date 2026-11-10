# conrad.de Balluff parser

The script behaves like a real user (visible browser, human typing speed, random pauses, scrolling,
one page at a time), so conrad.de has no reason to treat it as a bot and block it.

## How to run

1. Register a conrad.de account with **e-mail and password** (not "Continue with Google", PayPal or LinkedIn:
   the script cannot log in with those).

2. Install:

   ```bash
   python3 -m venv .venv
   .venv/bin/pip install -r requirements.txt
   PLAYWRIGHT_BROWSERS_PATH=0 .venv/bin/python -m playwright install chromium
   ```

3. Create the `.env` file:

   ```bash
   cp .env.example .env && chmod 600 .env
   ```

   Open `.env` and put in the e-mail and password from step 1:

   ```
   CONRAD_EMAIL=your-email@example.com
   CONRAD_PASSWORD=your-password
   ```

   If the password contains spaces or `#`, put it in quotes: `CONRAD_PASSWORD="my pass#word"`.

4. Start the script:

   ```bash
   .venv/bin/python conrad_parser.py
   ```

   A browser window opens and does the work (about 35 minutes). If Cloudflare shows a checkbox, click it.
   The result is saved to `balluff_products.csv`.

Steps 1–3 are needed only once. To run the script again, step 4 is enough.

## Login and data collection

**Login.** The script opens conrad.de in a visible browser and logs in through the site's own form
(Konto → Anmelden → e-mail → password), typing like a person. The e-mail and password are read from `.env`.

**Data collection.** The script opens the BALLUFF brand page, collects 100 product links (clicking "nächste"
for the next page), then opens each product with a random 5–10 second pause. From each product page it reads
the product details the page embeds for its own app (`__NUXT_DATA__`), the buy-box texts as shown on screen,
and all seller offers with their price tiers from the offers request the page makes by itself.
Each product is written to the CSV as soon as it has been read.
