import time
from click import option
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.common.exceptions import ElementClickInterceptedException, ElementNotInteractableException, StaleElementReferenceException
import subprocess
import random
import json
import os




####################################################################################################################################
# collects all car brands and models from yad2.co.il
# and saves them to a json file
# main purpose is to build a dictionary that will be used to build the correct urls to explorer cars by brand and model
###################################################################################################################################


def connect_nordvpn():
    try:
        subprocess.run(["nordvpn", "connect","israel"], check=True)
        print("Connected to NordVPN")
    except subprocess.CalledProcessError as e:
        print(f"Failed to connect to NordVPN: {e}")

def disconnect_nordvpn():
    try:
        subprocess.run(["nordvpn", "disconnect"], check=True)
        print("Disconnected from NordVPN")
    except subprocess.CalledProcessError as e:
        print(f"Failed to disconnect from NordVPN: {e}")

def random_sleep(base=2, jitter=1):
    time_to_sleep = base + random.uniform(-jitter, jitter)
    if time_to_sleep < 0:
        time_to_sleep = 0.1
    time.sleep(time_to_sleep)

def human_like_mouse_movements(driver, movements=5):
    # Move mouse randomly within the viewport to simulate human behavior
    for _ in range(movements):
        x = random.randint(0, 800)
        y = random.randint(0, 600)
        driver.execute_script(f"window.scrollTo({x}, {y});")
        random_sleep(0.3, 0.2)

def create_driver():
    options = uc.ChromeOptions()
    options.add_argument("--window-size=1920,1080")
    # Do not use headless mode to reduce detection risk
    # options.add_argument("--headless")

    # Rotate user agents from a small list
    user_agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.1 Safari/605.1.15",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Safari/537.36"
    ]
    user_agent = random.choice(user_agents)
    options.add_argument(f'user-agent={user_agent}')

    # Additional options to reduce detection
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-infobars")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-setuid-sandbox")

    driver = uc.Chrome(options=options)

    # Patch navigator properties to evade detection
    driver.execute_cdp_cmd('Page.addScriptToEvaluateOnNewDocument', {
        'source': '''
            // Pass the Chrome Test.
            window.chrome = {
                runtime: {},
                // etc.
            };
            // Pass the Permissions Test.
            const originalQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (parameters) => (
                parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
            );
            // Pass the Plugins Length Test.
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5],
            });
            // Pass the Languages Test.
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en'],
            });
            // Pass the webdriver Test.
            Object.defineProperty(navigator, 'webdriver', {
                get: () => false,
            });
        '''
    })

    return driver

def scrape_dropdowns_single_select(url):
    connect_nordvpn()
    print("Connected to NordVPN")
    driver = create_driver()
    all_data = []
    try:
        print(f"Loading URL: {url}")
        driver.get(url)
        random_sleep(4, 2) 

        human_like_mouse_movements(driver, movements=7)

        # Find all dropdown buttons
        toggle_buttons = driver.find_elements(By.CSS_SELECTOR, "button.dropdown_toggleButton__w4jW7, button[data-nagish='composed-dropdown-button']")
        print(f"Found {len(toggle_buttons)} dropdown buttons")

        if len(toggle_buttons) < 3:
            print("Not enough dropdown buttons found on the page.")
            return

        # Button 2 and Button 3 elements
        button2 = toggle_buttons[1]
        button3 = toggle_buttons[2]

        print("Clicking button2 to open dropdown")
        button2.click()
        random_sleep(2, 1)

        # resetBTN = driver.find_element(By.CSS_SELECTOR, "button.ok-reset-buttons_reset__SWVLu")

        #fetch all car brands 
        # options_container = driver.find_elements(By.CLASS_NAME,"image-checkbox_imageCheckbox___tM0b" )
        options_container = driver.find_elements(By.CLASS_NAME,"options-list_checkbox__10fMZ" ) #highest div contains label that contains input which holds the value
        
        print(f"Found {len(options_container)} options in button2 dropdown")
        for option in options_container: 
            print(option.text.strip())
            brand_name=option.text.strip()
            try:
                brand_input = option.find_element(By.CLASS_NAME,"check-button_input__YX3KQ")
                
                # brand_input = option.find_element(By.TAG_NAME,"input")
                print("*************printing option element*************")
                print(option.get_attribute("outerHTML"))
                # brand_input = option.find_element(By.CSS_SELECTOR, 'input[type="checkbox"]')
                brand_value = brand_input.get_attribute("value")
            except:
                brand_value = None
                print(f"[!] Failed to get value for brand: {brand_name}")

            # Loops through each car brand option
            print("clicking to select car brand option")
            option.click()
            random_sleep(4, 2)
            print("Clicking button3 to open sub-options")
            try:
                button3.click()
            except:
                print("Failed to click button3")
                option.click()
                random_sleep(2, 1)
                continue
            random_sleep(3, 1)

            model_inputs = driver.find_elements(By.CSS_SELECTOR, 'label[data-testid="check-button"] input[data-testid="vicon-check-item"]')
            models = []
            for i, model_input in enumerate(model_inputs):
                try:
                    value = model_input.get_attribute("value")
                    label = model_input.find_element(By.XPATH, "./ancestor::label")
                    # name = label.find_element(By.CSS_SELECTOR, "span").text.strip()
                    name = label.find_element(By.CSS_SELECTOR, "span").text.strip()

                    if not name:  # Skip empty names
                        print(f"[!] Skipping model at index {i} because name is empty")
                        continue

                    
                    print(f"{name}: {value}")
                    models.append({
                        "index": i,
                        "model_name": name,
                        "model_value": value
                    })
                except Exception as e:
                    print(f"[!] Failed at index {i}: {e}")


            all_data.append({
                "index": options_container.index(option),
                "brand": brand_name,
                "value": brand_value,
                "models": models
                })  
            # print(len(car_models))
            print("")
            print("Clicking button2 to reopen dropdown")
            button2.click()
            random_sleep(4, 2)
            print("Re-clicking car brand option to unselect it")
            option.click()
            random_sleep(4, 2)

    finally:
        print("Quitting driver and disconnecting NordVPN")
        driver.quit()
        disconnect_nordvpn()

    # Save all gathered data to a JSON file in the script's directory
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        output_path = os.path.join(script_dir, "scraped_data_single_select_stealth.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(all_data, f, ensure_ascii=False, indent=4)
        print(f"Scraped single select data saved to {output_path}")
    except Exception as e:
        print(f"Failed to save scraped single select data: {e}")

    for data in all_data:
        print(f"Button2 Option #{data['button2_index']} - Value: {data['button2_value']} - Label: {data['button2_label']}")
        print(f"Button3 Content:\n{data['button3_content']}\n{'-'*40}")

if __name__ == "__main__":
    URL = "https://www.yad2.co.il/vehicles/cars"
    scrape_dropdowns_single_select(URL)
