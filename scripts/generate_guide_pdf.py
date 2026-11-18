#!/usr/bin/env python3
"""Render the accompanying, deliberately small Markdown guide to eight A4 pages.

Run from qa-agent/ (no application dependency changes):
    python3 -m venv data/pdf-build/venv
    data/pdf-build/venv/bin/python -m pip install pymupdf
    data/pdf-build/venv/bin/python -B scripts/generate_guide_pdf.py

Default TrueType fonts are macOS Arial / Andale Mono. On another platform pass
--fonts-dir DIR containing Arial.ttf, Arial Bold.ttf and Andale Mono.ttf.
Only explicit <!-- pagebreak --> markers create pages; overflow is an error,
not a reason to silently clip text or reduce its font size.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import re

import pymupdf as pdf

ROOT = Path(__file__).resolve().parents[1]
INK = (0.13, 0.18, 0.25)
BLUE = (0.09, 0.29, 0.46)
LIGHT = (0.94, 0.96, 0.98)
MARGIN = 43
WIDTH, HEIGHT = 595.276, 841.89
CONTENT_WIDTH = WIDTH - 2 * MARGIN
BOTTOM = HEIGHT - 49


def blocks(text):
    """Parse only the headings, fences, paragraphs and tables used by the guide."""
    lines = text.strip().splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if line.startswith('```'):
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith('```'):
                code.append(lines[i])
                i += 1
            if i == len(lines):
                raise ValueError('Unclosed code fence')
            yield 'code', '\n'.join(code)
            i += 1
        elif line.startswith('|'):
            rows = []
            while i < len(lines) and lines[i].startswith('|'):
                row = [c.strip() for c in lines[i].strip('|').split('|')]
                if not all(re.fullmatch(r':?-+:?', c) for c in row):
                    rows.append(row)
                i += 1
            yield 'table', rows
        elif line.startswith('#'):
            level = len(line) - len(line.lstrip('#'))
            yield f'h{level}', line[level:].strip()
            i += 1
        elif line.startswith('- '):
            yield 'item', '• ' + line[2:]
            i += 1
        else:
            para = [line]
            i += 1
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(('#', '|', '```', '- ')):
                para.append(lines[i])
                i += 1
            yield 'paragraph', ' '.join(para)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--fonts-dir', type=Path, default=Path('/System/Library/Fonts/Supplemental'))
    args = parser.parse_args()
    paths = {'body': args.fonts_dir / 'Arial.ttf',
             'bold': args.fonts_dir / 'Arial Bold.ttf',
             'mono': args.fonts_dir / 'Andale Mono.ttf'}
    fonts = {name: pdf.Font(fontfile=str(path)) for name, path in paths.items()}

    def wrap(text, name, size, width):
        result = []
        for original in text.split('\n'):
            current = ''
            for word in original.split(' '):
                candidate = current + (' ' if current else '') + word
                if fonts[name].text_length(candidate, fontsize=size) <= width:
                    current = candidate
                else:
                    if current:
                        result.append(current)
                    current = word
                    # Long repository paths remain complete, with visual wrapping.
                    while fonts[name].text_length(current, fontsize=size) > width:
                        cut = 1
                        while cut < len(current) and fonts[name].text_length(current[:cut + 1], fontsize=size) <= width:
                            cut += 1
                        result.append(current[:cut])
                        current = current[cut:]
            result.append(current)
        return result

    source = ROOT / 'docs/GUIA_AGENTE_Y_API.md'
    target = source.with_suffix('.pdf')
    pages = source.read_text(encoding='utf-8').split('<!-- pagebreak -->')
    doc = pdf.open()
    for index, content in enumerate(pages, 1):
        page = doc.new_page(width=WIDTH, height=HEIGHT)
        for name, path in paths.items():
            page.insert_font(fontname=name, fontfile=str(path))
        y = 64.0

        def textline(text, x, top, name, size, color=INK):
            baseline = top + fonts[name].ascender * size
            page.insert_text((x, baseline), text, fontname=name, fontsize=size, color=color)

        textline('QA / GUÍA DEL AGENTE Y LA API', MARGIN, 25, 'bold', 8, BLUE)
        page.draw_line((MARGIN, 45), (WIDTH - MARGIN, 45), color=BLUE, width=0.8)
        for kind, value in blocks(content):
            if kind == 'table':
                n = len(value[0])
                ratios = {2: [0.31, 0.69], 3: [0.31, 0.24, 0.45]}[n]
                widths = [CONTENT_WIDTH * ratio for ratio in ratios]
                for row_index, row in enumerate(value):
                    name = 'bold' if row_index == 0 else 'body'
                    size, leading, padding = 9.5, 12.8, 6
                    cells = [wrap(cell, name, size, width - padding * 2) for cell, width in zip(row, widths)]
                    height = max(map(len, cells)) * leading + padding * 2
                    if y + height > BOTTOM:
                        raise ValueError(f'Table overflow on page {index}: {row}')
                    x = MARGIN
                    for cell, width in zip(cells, widths):
                        page.draw_rect(pdf.Rect(x, y, x + width, y + height), color=(0.8, 0.85, 0.89),
                                       fill=LIGHT if row_index % 2 == 0 else (1, 1, 1), width=0.4)
                        for j, line in enumerate(cell):
                            textline(line, x + padding, y + padding + j * leading, name, size)
                        x += width
                    y += height
                y += 10
                continue
            if kind == 'code':
                name, size, leading, gap = 'mono', 8.5, 10.3, 8
                lines = value.split('\n')
                for line in lines:
                    if fonts[name].text_length(line, fontsize=size) > CONTENT_WIDTH - 20:
                        raise ValueError(f'Code too wide on page {index}: {line}')
                height = len(lines) * leading + 14
                if y + height > BOTTOM:
                    raise ValueError(f'Code overflow on page {index}')
                page.draw_rect(pdf.Rect(MARGIN, y, WIDTH - MARGIN, y + height), color=None, fill=LIGHT)
                for j, line in enumerate(lines):
                    textline(line, MARGIN + 10, y + 6 + j * leading, name, size)
                y += height + gap
                continue
            styles = {'h1': ('bold', 22, 27, 11), 'h2': ('bold', 17, 21, 10),
                      'h3': ('bold', 11.5, 15, 6), 'item': ('body', 10, 13.0, 4),
                      'paragraph': ('body', 10.5, 13.5, 7)}
            name, size, leading, gap = styles[kind]
            lines = wrap(value, name, size, CONTENT_WIDTH)
            height = len(lines) * leading
            if y + height > BOTTOM:
                raise ValueError(f'Text overflow on page {index}: {value[:80]} (y={y:.1f})')
            for j, line in enumerate(lines):
                textline(line, MARGIN, y + j * leading, name, size, BLUE if kind.startswith('h') else INK)
            y += height + gap
        page.draw_line((MARGIN, HEIGHT - 37), (WIDTH - MARGIN, HEIGHT - 37), color=(0.8, 0.85, 0.89), width=0.5)
        textline('Documentación local · Datos sintéticos · Decisión final humana', MARGIN, HEIGHT - 28, 'body', 7.5)
        textline(f'{index} / {len(pages)}', WIDTH - MARGIN - 28, HEIGHT - 28, 'bold', 8)
        print(f'Page {index}: content ends at {y:.1f} pt (limit {BOTTOM:.1f})')
    doc.set_metadata({'title': 'Guía del agente QA y la API To-do', 'author': 'Documentación del proyecto',
                      'subject': 'Responsabilidades, operación, evidencia y límites', 'keywords': 'QA, API, MCP, pytest, español'})
    doc.subset_fonts()
    doc.save(target, garbage=4, deflate=True)
    doc.close()
    print(f'Created {target} ({target.stat().st_size:,} bytes)')


if __name__ == '__main__':
    main()
