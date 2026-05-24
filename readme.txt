# Yad2 Car Scraper - README

## Overview
This tool is a web scraper designed to automate the extraction of car listing information from the Yad2 classifieds website.

## What It Does
The scraper gathers structured data from listings, which typically includes:
- Vehicle make, model, and year
- Price
- Mileage
- Engine specifications
- Contact details
- Direct URLs to the listings

## Advanced Feature: Telegram Integration
You can enhance this scraper by integrating it with Telegram using the `pyTelegramBotAPI` (telebot). This allows you to:
- Receive notifications directly to your Telegram bot when new cars matching your criteria are found.
- Trigger scraping sessions remotely by sending commands to your bot.

### How to set up Telegram notifications:
1. **Create a Bot:** Use the BotFather on Telegram to create a new bot and obtain your API Token.
2. **Install Telebot:** Run `pip install pyTelegramBotAPI`.
3. **Configure the Bot:** - Add your API Token to your configuration file (`config.json` or `.env`).
   - Define the Chat ID where the bot should send notifications.
4. **Implement Requests:** - Within your script, initialize the bot using `telebot.TeleBot(TOKEN)`.
   - Add functions to send the scraped results to your Telegram chat using `bot.send_message(CHAT_ID, text)`.

## How to Use

### 1. Setup
- Ensure Python 3.x is installed.
- Clone the repository: `git clone https://github.com/ninjarun/yad2_car_scraper.git`
- Install dependencies: `pip install -r requirements.txt`

### 2. Configuration
- Check for configuration files to set your search criteria (brand, budget, etc.) and add your Telegram API credentials if using the bot feature.

### 3. Execution
- Run the main script: `python main.py`
- If Telegram integration is enabled, the results will be delivered to your Telegram chat.

## Important Considerations
- **Compliance:** Ensure your use of this tool aligns with the Yad2 Terms of Service. 
- **Performance:** Use reasonable delay intervals between requests.
- **Security:** Never share your Telegram Bot API token publicly. Keep it in an environment variable or secure config file.
