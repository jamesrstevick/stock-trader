"""
Load config.py even when Windows / OneDrive breaks a normal import.

Python's import uses open_code(), which can raise OSError 22 (Invalid argument)
on OneDrive placeholder files. Regular open() often still works. Gitignored
config.py is the usual victim; tracked .py files are already local.
"""

from __future__ import print_function

import os
import sys
import types

_ROOT = os.path.dirname(os.path.abspath(__file__))
_PATH = os.path.join(_ROOT, 'config.py')


def load():
    existing = sys.modules.get('config')
    if existing is not None and getattr(existing, '__file__', None):
        return existing
    if not os.path.isfile(_PATH):
        raise ImportError(
            'config.py not found in %s. Copy config.example.py to config.py '
            'and add your Schwab keys.' % _ROOT
        )
    try:
        import config as cfg
        return cfg
    except OSError:
        pass
    source = None
    last_err = None
    for enc in ('utf-8-sig', 'utf-8', 'cp1252'):
        try:
            with open(_PATH, 'r', encoding=enc) as f:
                source = f.read()
            break
        except (OSError, UnicodeDecodeError) as e:
            last_err = e
            continue
    if source is None:
        raise ImportError(
            'Could not read config.py (%s). OneDrive often causes this. '
            'Move the repo out of OneDrive (e.g. C:\\Users\\james\\stock-trader) '
            'or right-click the folder -> Always keep on this device.' % last_err
        )
    cfg = types.ModuleType('config')
    cfg.__file__ = _PATH
    exec(compile(source, _PATH, 'exec'), cfg.__dict__)
    sys.modules['config'] = cfg
    return cfg


config = load()
