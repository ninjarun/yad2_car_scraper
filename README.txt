Yad2 Cars Scraper + Telegram Alert Bot
======================================

This project scrapes car listings from Yad2, stores listings in SQLite, and provides a Telegram bot UI for choosing brands/models, running scans, managing subscriptions, exporting CSV/XLSX data, and handling paid plans through Stripe.

Project folder
--------------
/home/joe/PublicShare/yad2_cars_scraper

Main files
----------
- yad2.py
  Interactive/CLI scraper. Loads value4urlBuild.json, opens Chrome with undetected-chromedriver, scrapes selected Yad2 car brand/model pages, and stores listings in yad2_cars.db.

- tele_bot.py
  Telegram bot frontend. Lets users pick brands/models, create subscriptions, run scans, export results, and receive alerts. Starts a background scheduler/worker.

- tele_bot_stripe.py
  Stripe and Telegram payment handlers. Registers payment callbacks and runs a small Flask webhook server.

- db.py
  SQLite schema, listing storage, subscription storage, plans, CSV export, and helper queries.

- yad2dropDown.py
  Helper script for scraping Yad2 brand/model dropdown values and saving them as JSON.

- value4urlBuild.json
  Brand/model lookup data used to build Yad2 URLs and Telegram selection menus.

- main.py
  Currently empty; not an entry point.

Outputs created at runtime
--------------------------
- yad2_cars.db              SQLite database
- yad2_cars_data.csv        CSV export / scraper output, when generated
- exit_ip_history.txt       Proxy exit-IP history, when scraper runs
- chrome_profile_yad2/      Local Chrome profile directory, when scraper runs

Setup
-----
1. Create a virtual environment:

   python3 -m venv .venv
   source .venv/bin/activate

2. Install Python dependencies:

   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt

3. Install system/browser requirements:

   - Google Chrome or Chromium must be installed.
   - A graphical desktop/session is recommended; the scraper is not designed as pure headless automation.
   - If using NordVPN in yad2dropDown.py, the nordvpn CLI must be installed and logged in.
   - If using Bright Data / local proxy manager rotation in yad2.py, the local proxy ports must be running.

Environment variables
---------------------
Required for tele_bot.py:

   TELEGRAM_TOKEN=your_telegram_bot_token

Optional/test mode for tele_bot.py:

   QUEUE_TEST=1

Required for Bright Data proxy mode in yad2.py:

   BRD_PORTS=24000,24001,24002

Older/direct Bright Data proxy helper also references:

   BRD_HOST=...
   BRD_PORT=...
   BRD_USER=...
   BRD_PASS=...

Required for Stripe checkout/payments in tele_bot_stripe.py, if payments are enabled:

   STRIPE_SECRET_KEY=...
   STRIPE_PRICE_STARTER=...
   STRIPE_PRICE_PRO=...
   STRIPE_PRICE_DEALER=...
   STRIPE_WEBHOOK_SECRET=...
   STRIPE_WEBHOOK_PORT=9090
   TELEGRAM_PROVIDER_TOKEN=...

Run the scraper directly
------------------------
From the project folder:

   source .venv/bin/activate
   python yad2.py

The script asks for brand/model choices when run interactively. It writes listing data into yad2_cars.db.

Run the Telegram bot
--------------------
From the project folder:

   source .venv/bin/activate
   export TELEGRAM_TOKEN="your_bot_token"
   python tele_bot.py

The bot registers commands such as /start, /scan, /help, /my_subs, /myid, /get_plan, and admin-only plan/subscription commands.

Build/update brand-model JSON
-----------------------------
Run:

   source .venv/bin/activate
   python yad2dropDown.py

This script uses browser automation and may connect/disconnect NordVPN. Review it before running if VPN behavior is not desired.

Notes and cautions
------------------
- Scraping sites can trigger anti-bot pages. This project includes delays, browser fingerprint tweaks, and proxy rotation helpers.
- Do not commit real Telegram, Stripe, VPN, or proxy credentials.
- The database path is relative: yad2_cars.db is created in the current working directory. Run commands from the project folder for predictable results.
- Chrome automation usually needs a real display session. On a server/headless environment, use Xvfb or run from a desktop session.

Dependency summary
------------------
The requirements.txt file was created from a static scan of imports across:
- db.py
- tele_bot.py
- tele_bot_stripe.py
- yad2.py
- yad2dropDown.py

Third-party packages found:
- selenium
- undetected-chromedriver
- requests
- pandas
- scipy
- openpyxl
- python-telegram-bot
- stripe
- Flask
- click
