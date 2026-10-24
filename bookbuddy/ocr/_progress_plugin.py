"""ocrmypdf plugin (loaded with --plugin): reports page progress on stderr as 'BBOCR <done> <total>' lines."""
import sys

from ocrmypdf import hookimpl


class _Bar:
    def __init__(self, *, total=None, desc=None, unit=None, disable=False, **kwargs):
        self.total, self.desc, self.n = total, (desc or ""), 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def update(self, n=1, *, completed=None):
        self.n = completed if completed is not None else self.n + n
        if self.desc.strip().lower() == "ocr" and self.total:
            print(f"BBOCR {int(self.n)} {int(self.total)}", file=sys.stderr, flush=True)


@hookimpl
def get_progressbar_class():
    return _Bar
