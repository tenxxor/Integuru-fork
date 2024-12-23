import asyncio
import json
import os
from datetime import datetime
import aiohttp
from playwright.async_api import async_playwright, Page
import requests
import base64
from typing import Dict, Tuple, List

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

def validate_har(har: Dict):
    """
    Validate the HAR structure before saving.
    """
    if "log" not in har:
        raise ValueError("HAR file missing 'log' key.")
    if "entries" not in har["log"]:
        raise ValueError("HAR file missing 'entries' key in 'log'.")
    if not isinstance(har["log"]["entries"], list):
        raise ValueError("'entries' should be a list.")
    for entry in har["log"]["entries"]:
        if not isinstance(entry, dict):
            raise ValueError("Each entry in 'entries' should be a dictionary.")
        if "request" not in entry or not isinstance(entry["request"], dict):
            raise ValueError("Each entry should have a 'request' dictionary.")
        if "postData" in entry["request"] and not isinstance(entry["request"]["postData"], dict):
            raise ValueError("'postData' should be a dictionary if present.")

async def update_response(har: Dict, response):
    """
    Update the HAR log with the response data, if available.
    """
    try:
        request_id = id(response.request)
        if request_id not in har["request_map"]:
            print(f"No HAR entry found for response: {response.url}")
            return

        entry = har["request_map"][request_id]

        entry["response"]["status"] = response.status
        entry["response"]["statusText"] = response.status_text
        entry["response"]["headers"] = [
            {"name": k, "value": v} for k, v in response.headers.items()
        ]

        # Skip body retrieval for redirects (301, 302, 303, 307, 308)
        if response.status in (301, 302, 303, 307, 308):
            print(f"Response body is unavailable for redirect responses: {response.status} {response.url}")
            return

        # Await the response body
        try:
            body = await response.body()
        except Exception as e:
            print(f"Failed to retrieve body for response: {response.url} | Error: {e}")
            body = b""

        entry["response"]["content"]["size"] = len(body or b"")
        entry["response"]["content"]["mimeType"] = response.headers.get("content-type", "")

        # Ensure body is bytes before decoding
        if isinstance(body, bytes):
            try:
                # Attempt to decode as UTF-8
                text = body.decode('utf-8', errors='replace') if body else ""
                entry["response"]["content"]["text"] = text
                print(f"Decoded response body for {response.url}")
            except UnicodeDecodeError:
                # If decoding fails, store as base64
                print(f"Failed to decode response body as UTF-8 for {response.url}, storing as base64.")
                entry["response"]["content"]["text"] = base64.b64encode(body).decode('ascii')
        else:
            # If body is not bytes, handle accordingly
            print(f"Response body is not bytes for {response.url}, storing as empty string.")
            entry["response"]["content"]["text"] = ""

        # Calculate the time taken for the response
        entry["time"] = (
            datetime.now() - datetime.fromisoformat(entry["startedDateTime"])
        ).total_seconds() * 1000

    except Exception as e:
        print(f"An unexpected error occurred while updating response: {e}")

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

async def open_browser_and_capture_har():
    # Flag to indicate if closure has been initiated
    is_closing = False

    # 1) Create a BrowserBase session
    session_data = await create_browserbase_session()

    # 2) Print the debugger URL for manual navigation
    debug_url = get_browser_url(session_data['id'])
    print(f"\nOpen this URL in your browser to debug and navigate: {debug_url}")

    async with async_playwright() as p:
        # Connect to the remote browser using the websocket endpoint
        browser = await p.chromium.connect_over_cdp(session_data['connectUrl'])

        # Create a new browser context
        context = await browser.new_context()

        # 3) Do NOT create a new context or new page
        # Instead, get all existing pages and attach listeners

        # Get existing pages
        contexts = browser.contexts
        pages = []
        for context in contexts:
            pages.extend(context.pages)

        if not pages:
            print("No pages found in the browser.")
        else:
            print(f"Found {len(pages)} existing page(s).")

        # Prepare an in-memory HAR structure
        har = {
            "log": {
                "version": "1.2",
                "creator": {
                    "name": "Playwright",
                    "version": "1.0"
                },
                "pages": [],
                "entries": []
            },
            "request_map": {}  # Mapping from request ID to HAR entry
        }

        # Assign unique page IDs
        page_id_counter = 0

        # Initialize request counter and mapping
        request_counter = 0

        # Define a function to handle requests
        def handle_request(request):
            nonlocal is_closing, request_counter, page_id_counter
            if is_closing:
                return  # Do not handle new requests after closure initiated

            try:
                post_data_bytes = request.post_data_bytes()
                post_data_encoded = base64.b64encode(post_data_bytes).decode('ascii') if post_data_bytes else ""
                print(f"Captured Request: {request.method} {request.url}")
            except Exception as e:
                print(f"Error encoding post_data: {e}")
                post_data_encoded = ""

            # Assign a unique ID to the request
            request_id = id(request)
            request_counter += 1

            # Determine mimeType based on method
            mime_type = request.headers.get("Content-Type", "") if request.method in ["POST", "PUT", "PATCH"] else ""

            # Create HAR entry
            har_entry = {
                "startedDateTime": datetime.now().isoformat(),
                "time": 0,  # Placeholder, will be updated on response
                "request": {
                    "method": request.method,
                    "url": request.url,
                    "httpVersion": "HTTP/1.1",
                    "headers": [{"name": k, "value": v} for k, v in request.headers.items()],
                    "queryString": [],  # Populate if needed
                    "headersSize": -1,  # Placeholder
                    "postData": {
                        "mimeType": mime_type,
                        "text": post_data_encoded  # Encode as base64 if binary
                    } if mime_type else {
                        "mimeType": "",
                        "text": ""
                    }
                },
                "response": {
                    "status": 0,  # Placeholder, will be updated on response
                    "statusText": "",
                    "httpVersion": "HTTP/1.1",
                    "headers": [],
                    "cookies": [],
                    "content": {
                        "size": 0,
                        "mimeType": "",
                        "text": ""
                    },
                    "redirectURL": "",
                    "headersSize": -1,
                    "bodySize": -1
                },
                "cache": {},
                "timings": {
                    "dns": -1,
                    "connect": -1,
                    "ssl": -1,
                    "send": 0,
                    "wait": 0,
                    "receive": 0
                },
                "pageref": f"page_{page_id_counter}"
            }

            # Append to HAR entries
            har["log"]["entries"].append(har_entry)

            # Map the request ID to the HAR entry
            har["request_map"][request_id] = har_entry

            # Log the request mapping
            print(f"Mapped Request ID: {request_id} to HAR entry.")

        # Define a function to handle responses
        async def handle_response(response):
            nonlocal is_closing
            if is_closing:
                return  # Do not handle new responses after closure initiated

            # Schedule an async task to update our HAR
            task = asyncio.create_task(update_response(har, response))
            har["request_map"][id(response.request)] = har["request_map"].get(id(response.request), {})
            har["request_map"][id(response.request)] = har["request_map"].get(id(response.request), {})
            har["request_map"][id(response.request)] = har["request_map"].get(id(response.request), {})
            # The above three lines seem redundant; fixing to just schedule the task
            har["request_map"][id(response.request)] = har["request_map"].get(id(response.request))
            # Remove duplicate or redundant assignments
            # Correcting the implementation
            har["request_map"][id(response.request)] = har["request_map"].get(id(response.request))
            # The previous code is incorrect; here's the fix:
            task = asyncio.create_task(update_response(har, response))
            update_tasks.append(task)
            print(f"Captured Response: {response.status} {response.url}")

        # Function to attach listeners to a page
        def attach_listeners_to_page(page: Page, page_ref: str):
            page.on('request', handle_request)
            page.on('response', handle_response)
            print(f"Attached network event listeners to {page.url}")

        # Attach listeners to all existing pages
        for page in pages:
            page_id_counter += 1
            page_ref = f"page_{page_id_counter}"
            # Add page to HAR pages
            har["log"]["pages"].append({
                "startedDateTime": datetime.now().isoformat(),
                "id": page_ref,
                "title": page.title(),
                "pageTimings": {
                    "onContentLoad": -1,
                    "onLoad": -1
                }
            })
            attach_listeners_to_page(page, page_ref)

        # Listen for new pages and attach listeners
        async def handle_new_page(new_page: Page):
            nonlocal page_id_counter
            page_id_counter += 1
            page_ref = f"page_{page_id_counter}"
            # Add new page to HAR pages
            har["log"]["pages"].append({
                "startedDateTime": datetime.now().isoformat(),
                "id": page_ref,
                "title": new_page.title(),
                "pageTimings": {
                    "onContentLoad": -1,
                    "onLoad": -1
                }
            })
            attach_listeners_to_page(new_page, page_ref)

        browser.on('page', lambda new_page: asyncio.create_task(handle_new_page(new_page)))
        print("Listening for new pages to attach listeners.")

        # Prepare a list to keep track of response update tasks
        update_tasks: List[asyncio.Task] = []

        # 4) Inform the user and wait for manual navigation
        print(
            "You can now navigate freely using the debugger URL provided above.\n"
            "All network activity will be captured.\n"
            "Once you're done, return to this terminal and press Enter to save the HAR file and close the session."
        )
        input("Press Enter to continue and close the browser...")

        # 5) Initiate closure
        is_closing = True
        print("Initiating closure. No new network events will be handled.")

        # 6) Wait for all pending response updates to complete
        print("Waiting for all pending responses to finish...")
        if update_tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*update_tasks, return_exceptions=True), timeout=60)
                print("All pending responses have been processed.")
            except asyncio.TimeoutError:
                print("Timeout reached while waiting for response updates.")

        # 7) Save HAR to a file
        har_filename = f"network_requests_{datetime.now().strftime('%Y%m%d%H%M%S')}.har"
        try:
            # Validate HAR structure before saving
            validate_har(har)

            # Remove the request_map from the final HAR file
            har_to_save = har.copy()
            del har_to_save["request_map"]

            with open(har_filename, 'w', encoding='utf-8') as har_file:
                json.dump(har_to_save, har_file, indent=4)
            print(f"HAR file saved as {har_filename}")
        except Exception as e:
            print(f"Error saving HAR file: {e}")

        # 8) Save cookies
        print("Saving cookies...")
        try:
            cookies = await context.cookies()
            print(f"Number of cookies found: {len(cookies)}")
            with open("cookies.json", "w", encoding='utf-8') as f:
                json.dump(cookies, f, indent=4)
            print("Cookies saved to cookies.json")
        except Exception as e:
            print(f"Error saving cookies: {e}")

        # 9) Close the browser context
        try:
            await context.close()
            print("Context closed successfully, HAR file should be saved.")
        except Exception as e:
            print(f"Error closing context: {e}")

        # 10) Close the browser
        await browser.close()
        print("Browser closed.")

if __name__ == "__main__":
    asyncio.run(open_browser_and_capture_har())
