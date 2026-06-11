from app.services.web_context import (
    extract_html_document,
    extract_text_from_pdf,
    extract_text_from_html,
    normalize_text,
)


def test_extract_html_document_keeps_title_description_and_table():
    html = """
    <html>
      <head>
        <title>Vendor pricing</title>
        <meta name="description" content="Current pricing table for the platform">
      </head>
      <body>
        <script>ignore me</script>
        <h1>Pricing</h1>
        <p>Enterprise tier includes governance.</p>
        <table>
          <tr><th>Tier</th><th>Price</th></tr>
          <tr><td>Pro</td><td>$10</td></tr>
        </table>
      </body>
    </html>
    """
    title, text = extract_html_document(html)
    assert title == "Vendor pricing"
    assert "Description: Current pricing table" in text
    assert "Enterprise tier includes governance." in text
    assert "| Tier | Price |" in text
    assert "| Pro | $10 |" in text
    assert "ignore me" not in text


def test_extract_text_from_html_backcompat_returns_text_only():
    assert "Hello world" in extract_text_from_html("<html><body><p>Hello world</p></body></html>")


def test_normalize_text_fixes_spacing_before_punctuation():
    assert normalize_text("Hello   world  !") == "Hello world!"


def test_extract_text_from_pdf_reads_pages():
    import fitz

    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "PDF research content")
    content = doc.tobytes()
    doc.close()

    text = extract_text_from_pdf(content)
    assert "PDF research content" in text
    assert "Page 1" in text
