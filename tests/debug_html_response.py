"""
Debug script to see exactly what HTML we're receiving.
This will save the HTML response to a file so we can inspect it.
"""
import requests
from bs4 import BeautifulSoup

def debug_response():
    print("=" * 70)
    print("HTML RESPONSE DEBUGGER")
    print("=" * 70)
    
    # Create session with exact headers from Chrome
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7',
        'Accept-Language': 'en-US,en;q=0.9',
        # Removed Accept-Encoding to avoid compression issues
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache',
        'Priority': 'u=0, i',
        'sec-ch-ua': '"Google Chrome";v="141", "Not?A_Brand";v="8", "Chromium";v="141"',
        'sec-ch-ua-mobile': '?0',
        'sec-ch-ua-platform': '"Windows"',
        'sec-fetch-dest': 'document',
        'sec-fetch-mode': 'navigate',
        'sec-fetch-site': 'same-origin',
        'sec-fetch-user': '?1',
        'Upgrade-Insecure-Requests': '1',
        'Referer': 'https://www.festoolrecon.com/',
    })
    
    url = "https://www.festoolrecon.com/"
    
    print(f"\nFetching {url}...")
    
    try:
        response = session.get(url, timeout=30)
        print(f"Status: {response.status_code}")
        print(f"Content-Length: {len(response.content)} bytes")
        print(f"Content-Type: {response.headers.get('Content-Type')}")
        
        # Check for redirects
        if response.history:
            print(f"\nRedirects detected:")
            for i, resp in enumerate(response.history, 1):
                print(f"  {i}. {resp.status_code} -> {resp.url}")
            print(f"  Final: {response.url}")
        
        # Save HTML to file
        output_file = "debug_response.html"
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(response.text)
        print(f"\n✓ HTML saved to: {output_file}")
        
        # Parse and check for key elements
        soup = BeautifulSoup(response.text, 'lxml')
        
        print("\n" + "=" * 70)
        print("HTML ANALYSIS")
        print("=" * 70)
        
        # Check title
        title = soup.find('title')
        if title:
            print(f"\nPage Title: {title.get_text(strip=True)}")
        else:
            print("\n✗ No <title> tag found")
        
        # Check for product section
        product_section = soup.find('div', id='shopify-section-product-template')
        if product_section:
            print("✓ Found product section: #shopify-section-product-template")
        else:
            print("✗ Product section NOT FOUND: #shopify-section-product-template")
            
            # Search for alternatives
            print("\nSearching for alternative sections...")
            
            # Check all divs with IDs
            divs_with_id = soup.find_all('div', id=True)
            print(f"\nFound {len(divs_with_id)} <div> elements with id attributes:")
            for div in divs_with_id[:20]:  # Show first 20
                div_id = div.get('id', '')
                if 'product' in div_id.lower() or 'shopify' in div_id.lower():
                    print(f"  - {div_id}")
        
        # Check for product name
        product_names = soup.find_all('h1', class_='product-single__title')
        if product_names:
            print(f"\n✓ Found {len(product_names)} product name element(s)")
            for name in product_names[:3]:
                print(f"  - {name.get_text(strip=True)}")
        else:
            print("\n✗ No product name found with class 'product-single__title'")
            
            # Check for any h1
            h1_tags = soup.find_all('h1')
            if h1_tags:
                print(f"\nFound {len(h1_tags)} <h1> tag(s):")
                for h1 in h1_tags[:5]:
                    print(f"  - {h1.get_text(strip=True)[:100]}")
        
        # Check if we got blocked or got a challenge page
        text_lower = response.text.lower()
        if 'challenge' in text_lower or 'captcha' in text_lower:
            print("\n⚠️  WARNING: Possible CAPTCHA/challenge page detected")
        
        if 'cloudflare' in text_lower and len(response.text) < 5000:
            print("\n⚠️  WARNING: Possible Cloudflare challenge page")
        
        if 'access denied' in text_lower or 'forbidden' in text_lower:
            print("\n⚠️  WARNING: Access may be denied")
        
        # Show first 1000 characters
        print("\n" + "=" * 70)
        print("FIRST 1000 CHARACTERS OF HTML")
        print("=" * 70)
        print(response.text[:1000])
        print("...")
        
        print("\n" + "=" * 70)
        print(f"Full HTML saved to: {output_file}")
        print("Open this file in a text editor to inspect the complete response.")
        print("=" * 70)
        
    except Exception as e:
        print(f"\n✗ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    debug_response()