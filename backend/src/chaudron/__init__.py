"""Chaudron: household food stock management with AI recipe suggestions and receipt import.

Receipt *import*, deliberately not "receipt OCR". Nothing in this package performs
optical character recognition: a PDF order recap is read as embedded text, and a
photographed receipt is read by whichever vision model the household has
configured -- with no reading at all when it has configured none.
"""
