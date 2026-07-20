A simple multithreaded flask app which takes requests containing a doc file and converts it to a pdf using headless libreoffice.

Caveats: 
There is no security here, hide inside the network and wrap in TLS
Macros are still enabled, so a doc containing malicious ones could reach out. 
caveat emptor

TODO:
Learn if we need macros and turn them off if we don't

QuickStart:

docker build -t docx-converter:latest .

docker run -d --name docx-converter -p 8000:8000 -e MAX_CONCURRENT_CONVERSIONS=4 -e CONVERT_TIMEOUT_SECONDS=90 -e MAX_FILE_SIZE_BYTES=26214400 --memory=2g --cpus=2 docx-converter:latest

uv run pytest tests/test_app.py

uv run python stress_test.py --file tests/fixtures/sample.docx --requests 10 --concurrency 10
