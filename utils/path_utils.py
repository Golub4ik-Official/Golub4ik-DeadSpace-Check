import sys
import os


def is_frozen():
    return getattr(sys, 'frozen', False)


def app_dir():
    if is_frozen():
        return os.path.dirname(sys.executable)
    return os.getcwd()


def bundle_dir():
    if is_frozen():
        return sys._MEIPASS
    return os.getcwd()

