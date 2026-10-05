"""Atomic replacement tolerant of brief Windows reader/antivirus locks."""
import os
import time


def replace_with_retry(source, destination):
    for attempt in range(20):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.05)
