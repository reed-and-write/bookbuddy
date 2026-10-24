# DRM-protected books

Book Buddy never removes DRM. When a book is protected it stops with:

> This book is DRM-protected. Book Buddy can't read DRM-protected files.

## What counts as protected

- EPUBs whose `META-INF/encryption.xml` encrypts the content (font obfuscation alone is fine), or that carry Adobe
  ADEPT `rights.xml` or Apple FairPlay `sinf.xml`.
- PDFs that need a password to open, or whose text poppler refuses to extract. Owner-password-only PDFs that poppler
  can read work normally.
- Kindle files whose MOBI header says they are encrypted, Topaz books, and KFX DRMION containers.
- Any Calibre conversion failure that mentions DRM.

## What you can do

- Use a DRM-free copy of the book. Many shops sell DRM-free ebooks, and public-domain books (for example from
  Project Gutenberg or Standard Ebooks) are always DRM-free.
- If a PDF only needs a password you know, open it in a PDF reader and save an unprotected copy, then add that.
- If you think a book was flagged by mistake, please open an issue with the file type and the message you saw (never
  attach a book you don't have the right to share).
