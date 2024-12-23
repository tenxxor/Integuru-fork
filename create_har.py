import asyncio
import json
import os
from datetime import datetime
import aiohttp
import playwright
from playwright.async_api import async_playwright
import requests
import base64

# Configuration (you should store these in a config file or environment variables)
BROWSER_BASE_URL = 'https://api.browserbase.com'
BROWSER_BASE_PROJECT_ID = 'c7932661-116d-4147-a583-fcd34f6c2744'
BROWSER_BASE_API_KEY = 'bb_live_6gl2Inw8H1GclNkVYwQGnRjaNaI'
BROWSER_BASE_REGION = 'us-east-1'

def get_browser_url(session_id: str) -> str:
    """
    Get the URL to show the live view for the current browser session.

    :returns: URL
    """
    session_url = f"https://api.browserbase.com/v1/sessions/{session_id}/debug"
    headers = {
        "Content-Type": "application/json",
        "x-bb-api-key": BROWSER_BASE_API_KEY,
    }
    response = requests.get(session_url, headers=headers)

    # Raise an exception if there wasn't a good response from the endpoint
    response.raise_for_status()
    return response.json()["debuggerFullscreenUrl"]


async def update_response(har, response):
    try:
        # Find the corresponding request entry
        for entry in har["log"]["entries"]:
            if entry["request"]["url"] == response.url:
                entry["response"]["status"] = response.status
                entry["response"]["statusText"] = response.status_text
                entry["response"]["headers"] = [{"name": k, "value": v} for k, v in response.headers.items()]
                
                # Skip body retrieval for redirects
                if response.status in (301, 302, 303, 307, 308):
                    print("Response body is unavailable for redirect responses")
                    continue

                # Await the response body
                body = await response.body()
                
                entry["response"]["content"]["size"] = len(body or "")
                entry["response"]["content"]["mimeType"] = response.headers.get("content-type", "")
                
                # Attempt to decode the body
                try:
                    entry["response"]["content"]["text"] = body.decode('utf-8', errors='replace') if body else ""
                except UnicodeDecodeError:
                    print("Failed to decode response body as UTF-8, storing as binary.")
                    entry["response"]["content"]["text"] = base64.b64encode(body).decode('ascii')  # Store as base64 encoded string
                
                entry["time"] = (datetime.now() - datetime.fromisoformat(entry["startedDateTime"])).total_seconds() * 1000
                break
    except playwright._impl._errors.TargetClosedError:
        print("Target page, context, or browser has been closed before response body could be retrieved.")
    except playwright._impl._errors.Error as e:
        print(f"An error occurred while updating response: {e}")
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
                print(f"browserbase.com create session API took {(end - start).total_seconds()}s")

                if not response.ok:
                    error_text = await response.text()
                    print(f"Failed to create browserbase session. Status: {response.status}, Error: {error_text}")
                    raise Exception(f"Failed to create browserbase session: {response.status} {response.reason}")

                browserbase_session_data = await response.json()
                if not browserbase_session_data:
                    raise Exception('Empty response received from browserbase API')
                
                print(browserbase_session_data)

                return browserbase_session_data

    except Exception as error:
        print(f'Error creating browserbase session: {error}')
        if isinstance(error, aiohttp.ClientError):
            raise Exception(f"Network error connecting to browserbase API: {str(error)}")
        raise error




async def open_browser_and_wait():
    # Create a browserbase session
    session_data = await create_browserbase_session()
    
    # Print the debugger URL for manual navigation
    debug_url = get_browser_url(session_data['id'])
    print(f"\nOpen this URL in your browser to debug: {debug_url}")
    
    # Use the session data to connect to the remote browser
    async with async_playwright() as p:
        # Connect to the remote browser using the websocket endpoint
        browser = await p.chromium.connect_over_cdp(session_data['connectUrl'])

        # Get all browser contexts
        contexts = browser.contexts
        if not contexts:
            print("No contexts found, creating new context")
            # Generate a unique filename for the HAR file using the current timestamp
            timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
            har_filename = f"network_requests_{timestamp}.har"

            # Create a new browser context with the unique HAR file path
            context = await browser.new_context(
                record_har_path=har_filename,
                record_har_content="embed",
            )
        else:
            print("Using existing context")
            context = contexts[0]

        pages = context.pages
        page = pages[0] if pages else await context.new_page()

         # HAR structure
        har = {
            "log": {
                "version": "1.2",
                "creator": {
                    "name": "Playwright",
                    "version": "1.0"
                },
                "pages": [],
                "entries": []
            }
        }

        # Capture page load start time
        page_start_time = datetime.now().isoformat()

        # Add page information
        har["log"]["pages"].append({
            "startedDateTime": page_start_time,
            "id": "page_1",
            "title": "Example Page",
            "pageTimings": {
                "onContentLoad": -1,
                "onLoad": -1
            }
        })

        # Event listener for network requests
        page.on('request', lambda request: har["log"]["entries"].append({
            "startedDateTime": datetime.now().isoformat(),
            "time": 0,  # Placeholder, will be updated on response
            "request": {
                "method": request.method,
                "url": request.url,
                "httpVersion": "HTTP/1.1",
                "headers": [{"name": k, "value": v} for k, v in request.headers.items()],
                "queryString": [],
                "headersSize": -1,
                # "bodySize": len(request.post_data or b""),
                "postData": base64.b64encode(request.post_data).decode('ascii') if request.post_data else ""  # Encode as base64
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
            "pageref": "page_1"
        }))

         # Event listener for network responses
        page.on('response', lambda response: update_response(har, response))


        print(
            "Browser is open. Navigate in the debugger URL and press Enter when you're ready to save cookies..."
        )

        input("Press Enter to continue and close the browser and save cookies...")

        # Generate a unique filename for the HAR file using the current timestamp
        timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        har_filename = f"network_requests_{timestamp}.har"

        # Save the HAR data to a file
        with open(har_filename, 'w') as har_file:
            json.dump(har, har_file, indent=4)

        print(f"HAR file saved as {har_filename}")

        # Ensure 2FA is completed before saving cookies
        print("Saving cookies...")
        cookies = await context.cookies()
        print("Number of cookies found:", len(cookies))
        print("Cookies:", cookies)

        with open("cookies.json", "w") as f:
            json.dump(cookies, f, indent=4)

        # Close the context to ensure HAR file is saved
        try:
            await context.close()
            print("\n\nContext closed successfully, HAR file should be saved.")
        except Exception as e:
            print(f"\n\nError closing context: {e}")

        await browser.close()

if __name__ == "__main__":
    asyncio.run(open_browser_and_wait())



# async def generate_har_manually():
#     async with async_playwright() as p:
#         # Launch a browser
#         browser = await p.chromium.launch()
#         context = await browser.new_context()
#         page = await context.new_page()

#         # HAR structure
#         har = {
#             "log": {
#                 "version": "1.2",
#                 "creator": {
#                     "name": "Playwright",
#                     "version": "1.0"
#                 },
#                 "pages": [],
#                 "entries": []
#             }
#         }

#         # Capture page load start time
#         page_start_time = datetime.now().isoformat()

#         # Add page information
#         har["log"]["pages"].append({
#             "startedDateTime": page_start_time,
#             "id": "page_1",
#             "title": "Example Page",
#             "pageTimings": {
#                 "onContentLoad": -1,
#                 "onLoad": -1
#             }
#         })

#         # Event listener for network requests
#         page.on('request', lambda request: har["log"]["entries"].append({
#             "startedDateTime": datetime.now().isoformat(),
#             "time": 0,  # Placeholder, will be updated on response
#             "request": {
#                 "method": request.method,
#                 "url": request.url,
#                 "httpVersion": "HTTP/1.1",
#                 "headers": [{"name": k, "value": v} for k, v in request.headers.items()],
#                 "queryString": [],
#                 "headersSize": -1,
#                 "bodySize": len(request.post_data or b"") if isinstance(request.post_data, (bytes, str)) else 0

#             },
#             "response": {
#                 "status": 0,  # Placeholder, will be updated on response
#                 "statusText": "",
#                 "httpVersion": "HTTP/1.1",
#                 "headers": [],
#                 "cookies": [],
#                 "content": {
#                     "size": 0,
#                     "mimeType": "",
#                     "text": ""
#                 },
#                 "redirectURL": "",
#                 "headersSize": -1,
#                 "bodySize": -1
#             },
#             "cache": {},
#             "timings": {
#                 "dns": -1,
#                 "connect": -1,
#                 "ssl": -1,
#                 "send": 0,
#                 "wait": 0,
#                 "receive": 0
#             },
#             "pageref": "page_1"
#         }))

#         # Event listener for network responses
#         page.on('response', lambda response: update_response(har, response))

#         # Navigate to the desired URL
#         await page.goto('https://example.com')

#         # Perform any actions on the page as needed
#         # ...

#         # Close the browser
#         await browser.close()

#         # Generate a unique filename for the HAR file using the current timestamp
#         timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
#         har_filename = f"network_requests_{timestamp}.har"

#         # Save the HAR data to a file
#         with open(har_filename, 'w') as har_file:
#             json.dump(har, har_file, indent=4)

#         print(f"HAR file saved as {har_filename}")


