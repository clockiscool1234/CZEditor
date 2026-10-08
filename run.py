#!/usr/bin/env python3
import multiprocessing
import sys

from czqt.app import main

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
