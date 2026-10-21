# PDF documentation

- **[handbook.pdf](handbook.pdf)**: every guide in one document, with a contents page and diagrams.
- One PDF per guide: [architecture](architecture.pdf), [how-to](how-to.pdf), [setup](setup.pdf),
  [operations](operations.pdf), [security](security.pdf), [review-and-training](review-and-training.pdf),
  [evaluation](evaluation.pdf), [testing](testing.pdf).

The Markdown files in `docs/` are the source. After changing them, rebuild the PDFs:

```bash
pip install markdown                 # plus Google Chrome or Chromium, and poppler (pdftotext, pdfinfo)
python3 docs/pdf/source/build.py
```

The diagrams are drawn in `source/diagrams.py` and styled in `source/style.css`. The first build
downloads the fonts (Barlow Semi Condensed, Public Sans, IBM Plex Mono and IBM Plex Sans, all under
the SIL Open Font License) into `source/.fonts`; later builds work offline.
