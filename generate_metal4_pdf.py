#!/usr/bin/env python3
import markdown
from weasyprint import HTML, CSS

# Read the markdown file
with open('metal4_strategy.md', 'r', encoding='utf-8') as f:
    md_content = f.read()

# Convert markdown to HTML
html_content = markdown.markdown(md_content, extensions=['tables', 'fenced_code'])

# Wrap in basic HTML structure with styles
full_html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <title>Metal 4 Strategy Analysis</title>
    <style>
        body {{
            font-family: 'Helvetica', 'Arial', sans-serif;
            line-height: 1.6;
            color: #333;
            max-width: 800px;
            margin: 0 auto;
            padding: 20px;
        }}
        h1 {{
            color: #1a2a6c;
            border-bottom: 2px solid #1a2a6c;
            padding-bottom: 10px;
            text-align: center;
        }}
        h2 {{
            color: #b21f1f;
            margin-top: 30px;
            border-left: 5px solid #b21f1f;
            padding-left: 10px;
        }}
        h3 {{
            color: #444;
        }}
        code {{
            background-color: #f4f4f4;
            padding: 2px 5px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
        }}
        strong {{
            color: #000;
        }}
        hr {{
            border: 0;
            border-top: 1px solid #ccc;
            margin: 20px 0;
        }}
        ul {{
            padding-left: 20px;
        }}
        li {{
            margin-bottom: 10px;
        }}
    </style>
</head>
<body>
    {html_content}
</body>
</html>
"""

# Create PDF
html = HTML(string=full_html)
html.write_pdf('metal4_strategy.pdf')

print("PDF created successfully: metal4_strategy.pdf")
