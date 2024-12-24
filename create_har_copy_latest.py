import asyncio
import json
from datetime import datetime
from playwright.async_api import async_playwright
from playwright_har_tracer import HarTracer
import functools
import requests
import aiohttp
# Configuration (use environment variables or a secure config for sensitive data)
BROWSER_BASE_URL = 'https://api.browserbase.com'
BROWSER_BASE_PROJECT_ID = 'c7932661-116d-4147-a583-fcd34f6c2744'
BROWSER_BASE_API_KEY = 'bb_live_6gl2Inw8H1GclNkVYwQGnRjaNaI'
BROWSER_BASE_REGION = 'us-east-1'

def get_browser_url(session_id: str) -> str:
    """
    Get the URL to show the live view for the current browser session.
    """
    session_url = f"https://api.browserbase.com/v1/sessions/{session_id}/debug"
    headers = {
        "Content-Type": "application/json",
        "x-bb-api-key": BROWSER_BASE_API_KEY,
    }
    response = requests.get(session_url, headers=headers)
    response.raise_for_status()
    return response.json()["debuggerFullscreenUrl"]

async def create_browserbase_session(browser_params=None):
    if browser_params is None:
        browser_params = {}
    
    start = datetime.now()
    headers = {
        'x-bb-api-key': BROWSER_BASE_API_KEY,
        'Content-Type': 'application/json'
    }
    
    data = {
        'projectId': BROWSER_BASE_PROJECT_ID,
        'region': BROWSER_BASE_REGION
    }

    # Handle proxy configuration if provided
    if 'proxy_config' in browser_params:
        proxy = browser_params['proxy_config']
        data['proxies'] = [{
            'type': 'external',
            'server': f"http://{proxy['host']}:{proxy['port']}",
            'username': proxy['username'],
            'password': proxy['password']
        }]

    # Set viewport dimensions
    height = 1920
    width = 1080
    data['browserSettings'] = {
        'viewport': {
            'width': width,
            'height': height
        },
        'solveCaptchas': True  # Adjust based on your needs
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{BROWSER_BASE_URL}/v1/sessions",
                headers=headers,
                json=data
            ) as response:
                end = datetime.now()
                print(f"BrowserBase create session API took {(end - start).total_seconds()}s")

                if not response.ok:
                    error_text = await response.text()
                    print(f"Failed to create BrowserBase session. Status: {response.status}, Error: {error_text}")
                    raise Exception(f"Failed to create BrowserBase session: {response.status} {response.reason}")

                browserbase_session_data = await response.json()
                if not browserbase_session_data:
                    raise Exception('Empty response received from BrowserBase API')
                
                print("BrowserBase Session Data:", browserbase_session_data)
                return browserbase_session_data

    except Exception as error:
        print(f'Error creating BrowserBase session: {error}')
        if isinstance(error, aiohttp.ClientError):
            raise Exception(f"Network error connecting to BrowserBase API: {str(error)}")
        raise error

# Asynchronous input function
def sync_input(prompt):
    return input(prompt)

async def async_input(prompt):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(sync_input, prompt))

async def open_browser_and_capture_har():
    # Flag to indicate if closure has been initiated
    is_closing = False

    # 1) Create a BrowserBase session
    session_data = await create_browserbase_session()
    
    # 2) Print the debugger URL for manual navigation
    debug_url = get_browser_url(session_data['id'])
    print(f"\nOpen this URL in your browser to debug and navigate: {debug_url}")
    
    async with async_playwright() as p:
        # 3) Connect to the remote browser
        print(f"BrowserBase Connect URL: {session_data['connectUrl']}")
        browser = await p.chromium.connect_over_cdp(session_data['connectUrl'])
        print("Connected to remote browser.")
        
        # 4) Create a new browser context
        context = await browser.new_context()
        print("Created new browser context.")
    
        # 5) Initialize HAR tracer
        har_tracer = HarTracer(context, browser_name=p.chromium.name)
        print("HAR recording started.")
    
        # 6) Create a new page
        page = await context.new_page()
        print("Created a new page in the context.")
    
        # # 7) Optionally, perform an initial navigation to ensure network activity
        # await page.goto("https://www.google.com")
        # print("Navigated to https://www.example.com to generate network activity.")
    
        # # 8) Inform the user and wait for manual navigation
        # print(
        #     "You can now navigate freely using the debugger URL provided above.\n"
        #     "All network activity will be captured.\n"
        #     "Once you're done, return to this terminal and press Enter to save the HAR file and close the session."
        # )
        await async_input("Press Enter to continue and close the browser...")
    
        # 9) Initiate closure
        is_closing = True
        print("Initiating closure. Stopping HAR recording...")
    
        # 10) Stop HAR recording and retrieve HAR data
        har_data = await har_tracer.flush()
        print("HAR recording stopped.")
        await asyncio.sleep(10)  # Wait for 10 second
    
        # 11) Save HAR to a file
        har_filename = f"network_requests_{datetime.now().strftime('%Y%m%d_%H%M%S')}.har"
        try:
            with open(har_filename, 'w', encoding='utf-8') as har_file:
                har_file.write(har_data.to_json())
            print(f"HAR file saved as {har_filename}")
        except Exception as e:
            print(f"Error saving HAR file: {e}")
    
        # 12) Save cookies
        print("Saving cookies...")
        try:
            cookies = await context.cookies()
            print(f"Number of cookies found: {len(cookies)}")
            with open("cookies.json", "w", encoding='utf-8') as f:
                json.dump(cookies, f, indent=4)
            print("Cookies saved to cookies.json")
        except Exception as e:
            print(f"Error saving cookies: {e}")
    
        # 13) Close the browser context
        try:
            await context.close()
            print("Context closed successfully, HAR file should be saved.")
        except Exception as e:
            print(f"Error closing context: {e}")
    
        # 14) Close the browser
        await browser.close()
        print("Browser closed.")

if __name__ == "__main__":
    asyncio.run(open_browser_and_capture_har())
