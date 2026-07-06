import io
import os
os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = 'algebraic-cycle-432817-r8-ae9fa17cac37.json'

from google.cloud import vision
# from google.cloud import types

def detect_text(path):
    """Detects text in the file."""
    client = vision.ImageAnnotatorClient()

    with io.open(path, 'rb') as image_file:
        content = image_file.read()

    image = vision.Image(content=content)

    response = client.text_detection(image=image)
    texts = response.text_annotations

    for text in texts:
        print(text.description)

if __name__ == '__main__':
    # Replace 'path/to/your/image.jpg' with the actual path to your image
    detect_text('captured_image.png')