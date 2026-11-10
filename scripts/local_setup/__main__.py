"""python -m local_setup。脚本目录需要在 Python 路径上。"""
import sys

from .installer import main


if __name__ == "__main__":
    sys.exit(main())
