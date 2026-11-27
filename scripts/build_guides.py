"""Сборка руководств docs/guides/*.md → DOCX (pandoc + фирменный reference.docx) и PDF (pandoc → HTML → Chrome headless).

Запуск: python scripts/build_guides.py   (нужны pandoc и Google Chrome)
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from docx import Document
from docx.shared import Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
GUIDES = ROOT / 'docs' / 'guides'
RED = RGBColor(0xA2, 0x0D, 0x32)
CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome'

CSS = """
@page { size: A4; margin: 18mm 16mm 20mm; @bottom-right { content: counter(page); } }
body { font-family: 'Montserrat', 'Helvetica Neue', Arial, sans-serif; font-size: 11pt; color: #1E1E1E; line-height: 1.45; }
header#title-block-header { border-bottom: 4px solid #A20D32; margin-bottom: 18px; padding-bottom: 10px; }
header#title-block-header::before { content: 'софтлайн'; color: #A20D32; font-weight: 800; font-size: 13pt; letter-spacing: .5px; }
h1.title { color: #A20D32; font-size: 20pt; margin: 6px 0 2px; } p.subtitle { color: #6B6B6E; margin: 0; font-size: 12pt; }
h1 { color: #A20D32; font-size: 15pt; margin-top: 22px; border-bottom: 1px solid #E5E5E7; padding-bottom: 3px; }
h2 { color: #1E1E1E; font-size: 12.5pt; margin-top: 16px; }
table { border-collapse: collapse; width: 100%; margin: 8px 0 12px; font-size: 10pt; }
th { background: #A20D32; color: #fff; text-align: left; padding: 5px 7px; }
td { border-bottom: 1px solid #E5E5E7; padding: 5px 7px; vertical-align: top; }
tr:nth-child(even) td { background: #FAFAFB; }
code { background: #F3F3F5; padding: 1px 4px; border-radius: 3px; font-size: 9.5pt; }
pre { background: #F3F3F5; padding: 10px; border-left: 3px solid #A20D32; font-size: 9pt; white-space: pre-wrap; }
a { color: #A20D32; }
img { max-width: 100%; border: 1px solid #E5E5E7; border-radius: 6px; margin: 6px 0 10px; }
"""


def reference_docx(path: Path) -> None:
    subprocess.run(['pandoc', '-o', str(path), '--print-default-data-file', 'reference.docx'], check=True)
    d = Document(str(path))
    for name in ('Normal', 'Body Text', 'First Paragraph', 'Compact'):
        if name in [s.name for s in d.styles]:
            st = d.styles[name]
            st.font.name, st.font.size = 'Arial', Pt(11)
    for name, size in (('Title', 22), ('Subtitle', 13), ('Heading 1', 16), ('Heading 2', 13), ('Heading 3', 12)):
        st = d.styles[name]
        st.font.name, st.font.size, st.font.bold = 'Arial', Pt(size), name != 'Subtitle'
        st.font.color.rgb = RED if name != 'Subtitle' else RGBColor(0x6B, 0x6B, 0x6E)
    d.save(str(path))


def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    ref = tmp / 'ref.docx'
    reference_docx(ref)
    css = tmp / 'style.css'
    css.write_text(CSS, encoding='utf-8')
    for md in sorted(GUIDES.glob('*.md')):
        docx = md.with_suffix('.docx')
        subprocess.run(['pandoc', str(md), '-o', str(docx), f'--reference-doc={ref}', f'--resource-path={GUIDES}', '--toc', '--toc-depth=1',
                        '-M', 'toc-title=Содержание', '-M', 'lang=ru'], check=True)
        html = tmp / (md.stem + '.html')
        subprocess.run(['pandoc', str(md), '-s', '-o', str(html), '--embed-resources', f'--resource-path={GUIDES}', f'--css={css}', '-M', 'lang=ru'], check=True)
        pdf = md.with_suffix('.pdf')
        subprocess.run([CHROME, '--headless=new', '--disable-gpu', '--no-pdf-header-footer', f'--print-to-pdf={pdf}', html.as_uri()],
                       check=True, capture_output=True)
        print(docx.name, pdf.name, pdf.stat().st_size)


if __name__ == '__main__':
    main()
