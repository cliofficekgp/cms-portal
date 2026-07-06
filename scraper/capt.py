from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
import base64
from PIL import Image
from io import BytesIO
import time

# Initialize the WebDriver (make sure you have the correct driver for your browser)
driver = webdriver.Chrome(executable_path='chromedriver-win64\chromedriver.exe')

# Open the webpage
driver.get('https://cms.indianrail.gov.in/CMSREPORT/JSP/rpt/LoginAction.do?hmode=login&isResponsive=Y')

time.sleep(100)
# Wait for the image element to be present
try:
    image_element = WebDriverWait(driver, 10).until(
        EC.presence_of_element_located((By.ID, "capt"))
    )

    # Get the src attribute
    src_attribute = image_element.get_attribute('src')

    # Print the src attribute to debug
    print("SRC Attribute:", src_attribute)

    if src_attribute and ',' in src_attribute:
        # Extract the base64 string (remove the prefix)
        base64_image = src_attribute.split(',')[1]

        # Decode the base64 string into bytes
        image_data = base64.b64decode(base64_image)

        # Convert the byte data into an image and save it
        image = Image.open(BytesIO(image_data))
        image.save('captured_image.jpg')
        print("Image saved successfully!")
    else:
        print("The src attribute is empty or incorrectly formatted.")

finally:
    # Close the browser
    driver.quit()
