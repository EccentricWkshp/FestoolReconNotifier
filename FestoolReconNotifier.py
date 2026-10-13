import requests
from bs4 import BeautifulSoup
import time

headers = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
}

def fetch_all_products():
    base_url = "https://www.festoolrecon.com"
    try:
        response = requests.get(base_url, headers=headers)
        response.raise_for_status()  # Raise an error for bad status codes
        soup = BeautifulSoup(response.content, 'html.parser')

        # Check for 'Click here for additional offerings' link
        additional_offerings_link = soup.find('a', class_='collection-card')
        if additional_offerings_link and 'href' in additional_offerings_link.attrs:
            all_products_url = base_url + additional_offerings_link['href']
            products = fetch_all_pages(all_products_url)
        else:
            products = fetch_products_from_page(base_url)

        # Print all products in the desired format
        for name, regular_price, sale_price in products:
            print(f"{name} // {regular_price} // {sale_price}")

    except requests.exceptions.RequestException as e:
        print(f"Error fetching the main page: {e}")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")

def fetch_all_pages(url):
    """
    Fetch all pages from the given URL.
    """
    products = []
    current_page = 1
    while True:
        page_url = f"{url}?page={current_page}"
        print(f"Fetching {page_url}")
        try:
            response = requests.get(page_url, headers=headers)
            response.raise_for_status()  # Raise an error for bad status codes
            soup = BeautifulSoup(response.content, 'html.parser')
        
            # Find all product containers
            product_containers = soup.find_all('a', class_='product-card')
            if not product_containers:
                break
            
            for product in product_containers:
                name_tag = product.find('div', class_='product-card__name')
                price_tag = product.find('div', class_='product-card__price')
                if name_tag and price_tag:
                    name = name_tag.text.strip()
                    prices = [price for price in price_tag.stripped_strings]
                    regular_price = prices[1] if len(prices) > 2 else "N/A"
                    sale_price = prices[-1]
                    products.append((name, regular_price, sale_price))
            
            # Check if there is a next page
            next_page = soup.find('span', class_='next')
            if next_page and next_page.find('a'):
                current_page += 1
                time.sleep(1)  # Sleep to avoid being blocked
            else:
                break

        except requests.exceptions.RequestException as e:
            print(f"Error fetching page {current_page}: {e}")
            break
        except Exception as e:
            print(f"An unexpected error occurred on page {current_page}: {e}")
            break

    return products

def fetch_products_from_page(url):
    """
    Fetch products from a single page URL.
    """
    products = []
    try:
        response = requests.get(url, headers=headers)
        response.raise_for_status()  # Raise an error for bad status codes
        soup = BeautifulSoup(response.content, 'html.parser')

        # Find all product containers
        product_containers = soup.find_all('a', class_='product-card')
        for product in product_containers:
            name_tag = product.find('div', class_='product-card__name')
            price_tag = product.find('div', class_='product-card__price')
            if name_tag and price_tag:
                name = name_tag.text.strip()
                prices = [price for price in price_tag.stripped_strings]
                regular_price = prices[1] if len(prices) > 2 else "N/A"
                sale_price = prices[-1]
                products.append((name, regular_price, sale_price))

    except requests.exceptions.RequestException as e:
        print(f"Error fetching products from page: {e}")
    except Exception as e:
        print(f"An unexpected error occurred while fetching products from page: {e}")

    return products

if __name__ == "__main__":
    fetch_all_products()
