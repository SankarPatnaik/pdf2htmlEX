#!/usr/bin/env python3
"""
PDF to HTML Converter Module

A reliable Python module for converting PDF files to standalone HTML with embedded resources.
Uses PyMuPDF (fitz) for maximum compatibility and reliability.

PyMuPDF>=1.23.0
Pillow>=9.0.0
"""

import fitz  # PyMuPDF
import base64
import io
import os
import html
import logging
from pathlib import Path
from typing import Optional, Dict, List, Tuple
import re


class PDFToHTMLConverter:
    """
    A robust PDF to HTML converter that creates standalone HTML files.

    Features:
    - Extracts text with formatting preservation
    - Embeds images as base64 data URIs
    - Maintains document structure
    - Handles various PDF types reliably
    - Creates self-contained HTML files
    """

    def __init__(self, embed_fonts: bool = True, preserve_layout: bool = True):
        """
        Initialize the converter.

        Args:
            embed_fonts: Whether to embed font information in CSS
            preserve_layout: Whether to preserve original PDF layout
        """
        self.embed_fonts = embed_fonts
        self.preserve_layout = preserve_layout
        self.logger = logging.getLogger(__name__)

        # Setup logging
        logging.basicConfig(level=logging.INFO)

    def convert_pdf_to_html(
        self, pdf_path: str, output_path: Optional[str] = None
    ) -> str:
        """
        Convert a PDF file to standalone HTML.

        Args:
            pdf_path: Path to the input PDF file
            output_path: Optional path for output HTML file

        Returns:
            Path to the generated HTML file

        Raises:
            FileNotFoundError: If PDF file doesn't exist
            Exception: For other conversion errors
        """
        if not os.path.exists(pdf_path):
            raise FileNotFoundError(f"PDF file not found: {pdf_path}")

        try:
            # Open PDF document
            doc = fitz.open(pdf_path)

            # Extract content from all pages
            html_content = self._extract_content_from_pdf(doc)

            # Generate complete HTML
            full_html = self._generate_complete_html(html_content, pdf_path)

            # Determine output path
            if output_path is None:
                pdf_name = Path(pdf_path).stem
                output_path = f"{pdf_name}.html"

            # Write HTML file
            with open(output_path, "w", encoding="utf-8") as f:
                f.write(full_html)

            doc.close()
            self.logger.info(f"Successfully converted {pdf_path} to {output_path}")
            return output_path

        except Exception as e:
            self.logger.error(f"Error converting PDF to HTML: {str(e)}")
            raise

    def _extract_content_from_pdf(self, doc: fitz.Document) -> Dict:
        """
        Extract all content from PDF document.

        Args:
            doc: PyMuPDF document object

        Returns:
            Dictionary containing extracted content
        """
        content = {"pages": [], "images": [], "fonts": set(), "metadata": doc.metadata}

        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            page_content = self._extract_page_content(page, page_num)
            content["pages"].append(page_content)

        return content

    def _extract_page_content(self, page: fitz.Page, page_num: int) -> Dict:
        """
        Extract content from a single page.

        Args:
            page: PyMuPDF page object
            page_num: Page number

        Returns:
            Dictionary containing page content
        """
        # Get page dimensions
        page_rect = page.rect

        # Extract text blocks with formatting
        text_blocks = self._extract_text_blocks(page)

        # Extract images
        images = self._extract_images(page, page_num)

        # Extract drawings/vector graphics
        drawings = self._extract_drawings(page)

        return {
            "page_num": page_num,
            "dimensions": {"width": page_rect.width, "height": page_rect.height},
            "text_blocks": text_blocks,
            "images": images,
            "drawings": drawings,
        }

    def _extract_text_blocks(self, page: fitz.Page) -> List[Dict]:
        """
        Extract text blocks with formatting information.

        Args:
            page: PyMuPDF page object

        Returns:
            List of text blocks with formatting
        """
        text_blocks = []

        # Get text as dictionary with formatting
        text_dict = page.get_text("dict")

        for block in text_dict["blocks"]:
            if "lines" in block:  # Text block
                block_data = {"type": "text", "bbox": block["bbox"], "lines": []}

                for line in block["lines"]:
                    line_data = {"bbox": line["bbox"], "spans": []}

                    for span in line["spans"]:
                        # Extract font information
                        font_info = {
                            "font": span.get("font", ""),
                            "size": span.get("size", 12),
                            "color": span.get("color", 0),
                            "bold": "bold" in span.get("font", "").lower(),
                            "italic": "italic" in span.get("font", "").lower(),
                        }

                        span_data = {
                            "text": span.get("text", ""),
                            "bbox": span.get("bbox", [0, 0, 0, 0]),
                            "font_info": font_info,
                        }

                        line_data["spans"].append(span_data)

                    block_data["lines"].append(line_data)

                text_blocks.append(block_data)

        return text_blocks

    def _extract_images(self, page: fitz.Page, page_num: int) -> List[Dict]:
        """
        Extract images from page and convert to base64.

        Args:
            page: PyMuPDF page object
            page_num: Page number

        Returns:
            List of image data with base64 encoding
        """
        images = []
        image_list = page.get_images()

        for img_index, img in enumerate(image_list):
            try:
                # Get image data
                xref = img[0]
                pix = fitz.Pixmap(page.parent, xref)

                # Convert to PNG if not already
                if pix.n - pix.alpha < 4:  # Can convert to PNG
                    img_data = pix.tobytes("png")
                    img_format = "png"
                else:  # Convert to JPEG
                    pix_rgb = fitz.Pixmap(fitz.csRGB, pix)
                    img_data = pix_rgb.tobytes("jpeg")
                    img_format = "jpeg"
                    pix_rgb = None

                # Encode as base64
                img_b64 = base64.b64encode(img_data).decode()

                # Get image position on page
                img_rects = page.get_image_rects(xref)

                image_info = {
                    "index": img_index,
                    "page": page_num,
                    "format": img_format,
                    "data": img_b64,
                    "width": pix.width,
                    "height": pix.height,
                    "rects": img_rects,
                }

                images.append(image_info)
                pix = None

            except Exception as e:
                self.logger.warning(
                    f"Could not extract image {img_index} from page {page_num}: {str(e)}"
                )
                continue

        return images

    def _extract_drawings(self, page: fitz.Page) -> List[Dict]:
        """
        Extract vector drawings and paths.

        Args:
            page: PyMuPDF page object

        Returns:
            List of drawing elements
        """
        drawings = []

        # Get drawing commands
        try:
            paths = page.get_drawings()
            for path in paths:
                drawing_info = {
                    "type": "drawing",
                    "bbox": path.get("rect", [0, 0, 0, 0]),
                    "stroke_color": path.get("color", None),
                    "fill_color": path.get("fill", None),
                    "width": path.get("width", 1),
                }
                drawings.append(drawing_info)
        except Exception as e:
            self.logger.warning(f"Could not extract drawings: {str(e)}")

        return drawings

    def _generate_complete_html(self, content: Dict, pdf_path: str) -> str:
        """
        Generate complete HTML document with embedded resources.

        Args:
            content: Extracted PDF content
            pdf_path: Original PDF file path

        Returns:
            Complete HTML string
        """
        pdf_name = Path(pdf_path).stem

        # Generate CSS
        css = self._generate_css(content)

        # Generate HTML body
        body_html = self._generate_body_html(content)

        # Create complete HTML
        html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html.escape(pdf_name)}</title>
    <style>
{css}
    </style>
</head>
<body>
    <div class="pdf-container">
        <h1 class="pdf-title">{html.escape(pdf_name)}</h1>
{body_html}
    </div>
</body>
</html>"""

        return html_template

    def _generate_css(self, content: Dict) -> str:
        """
        Generate CSS styles for the HTML document.

        Args:
            content: Extracted PDF content

        Returns:
            CSS string
        """
        css = """
        body {
            font-family: Arial, sans-serif;
            margin: 0;
            padding: 20px;
            background-color: #f5f5f5;
        }
        
        .pdf-container {
            max-width: 1200px;
            margin: 0 auto;
            background-color: white;
            box-shadow: 0 0 10px rgba(0, 0, 0, 0.1);
            border-radius: 8px;
            overflow: hidden;
        }
        
        .pdf-title {
            background-color: #2c3e50;
            color: white;
            padding: 20px;
            margin: 0;
            font-size: 24px;
            font-weight: bold;
        }
        
        .pdf-page {
            padding: 20px;
            border-bottom: 2px solid #ecf0f1;
            position: relative;
            min-height: 400px;
        }
        
        .pdf-page:last-child {
            border-bottom: none;
        }
        
        .page-number {
            position: absolute;
            top: 10px;
            right: 20px;
            background-color: #3498db;
            color: white;
            padding: 5px 10px;
            border-radius: 15px;
            font-size: 12px;
            font-weight: bold;
        }
        
        .text-block {
            margin: 10px 0;
            line-height: 1.4;
        }
        
        .text-span {
            display: inline;
        }
        
        .bold {
            font-weight: bold;
        }
        
        .italic {
            font-style: italic;
        }
        
        .pdf-image {
            max-width: 100%;
            height: auto;
            display: block;
            margin: 15px 0;
            border: 1px solid #ddd;
            border-radius: 4px;
        }
        
        .image-container {
            text-align: center;
            margin: 20px 0;
        }
        
        .drawing-element {
            position: absolute;
            border: 1px solid #ccc;
        }
        
        @media print {
            .pdf-container {
                box-shadow: none;
                border-radius: 0;
            }
            
            .pdf-page {
                page-break-after: always;
            }
            
            .pdf-page:last-child {
                page-break-after: auto;
            }
        }
        """

        return css

    def _generate_body_html(self, content: Dict) -> str:
        """
        Generate HTML body content from extracted PDF content.

        Args:
            content: Extracted PDF content

        Returns:
            HTML body string
        """
        html_parts = []

        for page_data in content["pages"]:
            page_html = self._generate_page_html(page_data)
            html_parts.append(page_html)

        return "\n".join(html_parts)

    def _generate_page_html(self, page_data: Dict) -> str:
        """
        Generate HTML for a single page.

        Args:
            page_data: Page content data

        Returns:
            HTML string for the page
        """
        page_num = page_data["page_num"]

        html_parts = [
            f'        <div class="pdf-page" id="page-{page_num + 1}">',
            f'            <div class="page-number">Page {page_num + 1}</div>',
        ]

        # Add text blocks
        for block in page_data["text_blocks"]:
            if block["type"] == "text":
                block_html = self._generate_text_block_html(block)
                html_parts.append(f"            {block_html}")

        # Add images
        for image in page_data["images"]:
            image_html = self._generate_image_html(image)
            html_parts.append(f"            {image_html}")

        html_parts.append("        </div>")

        return "\n".join(html_parts)

    def _generate_text_block_html(self, block: Dict) -> str:
        """
        Generate HTML for a text block.

        Args:
            block: Text block data

        Returns:
            HTML string for the text block
        """
        html_parts = ['<div class="text-block">']

        for line in block["lines"]:
            line_parts = []

            for span in line["spans"]:
                text = html.escape(span["text"])
                font_info = span["font_info"]

                # Build CSS classes
                classes = ["text-span"]
                if font_info["bold"]:
                    classes.append("bold")
                if font_info["italic"]:
                    classes.append("italic")

                # Build inline styles
                styles = []
                if font_info["size"] != 12:
                    styles.append(f"font-size: {font_info['size']}px")

                # Convert color (assuming black text if color is 0)
                if font_info["color"] != 0:
                    color_hex = f"#{font_info['color']:06x}"
                    styles.append(f"color: {color_hex}")

                # Create span element
                class_attr = f' class="{" ".join(classes)}"' if classes else ""
                style_attr = f' style="{"; ".join(styles)}"' if styles else ""

                span_html = f"<span{class_attr}{style_attr}>{text}</span>"
                line_parts.append(span_html)

            # Join spans for this line
            if line_parts:
                html_parts.append("".join(line_parts))

        html_parts.append("</div>")

        return "\n".join(html_parts)

    def _generate_image_html(self, image: Dict) -> str:
        """
        Generate HTML for an image.

        Args:
            image: Image data

        Returns:
            HTML string for the image
        """
        data_uri = f"data:image/{image['format']};base64,{image['data']}"

        return f"""<div class="image-container">
                <img src="{data_uri}" 
                     alt="PDF Image {image['index']}" 
                     class="pdf-image"
                     width="{image['width']}" 
                     height="{image['height']}">
            </div>"""


def convert_pdf_to_html(
    pdf_path: str,
    output_path: Optional[str] = None,
    embed_fonts: bool = True,
    preserve_layout: bool = True,
) -> str:
    """
    Convenience function to convert PDF to HTML.

    Args:
        pdf_path: Path to input PDF file
        output_path: Optional output HTML file path
        embed_fonts: Whether to embed font information
        preserve_layout: Whether to preserve original layout

    Returns:
        Path to generated HTML file
    """
    converter = PDFToHTMLConverter(
        embed_fonts=embed_fonts, preserve_layout=preserve_layout
    )
    return converter.convert_pdf_to_html(pdf_path, output_path)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: python pdf_to_html.py <pdf_file> [output_file]")
        sys.exit(1)

    pdf_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        result = convert_pdf_to_html(pdf_file, output_file)
        print(f"Successfully converted PDF to HTML: {result}")
    except Exception as e:
        print(f"Error: {str(e)}")
        sys.exit(1)
