# !quote — reply with a random quote from quotes.txt.
#
# the quote pool lives in commands/quotes.txt. one quote per line and
# '#' comments are ignored. the file is read on every invocation, so
# edits take effect immediately with no reload.
# 
# this could be used to do things other than return random quotes,
# basically anything where you want a random response returned from
# a given data set.
#
# todo: use quotes.txt to see db table, then use db going forward.
#

import os
import random

NAME = "quote"
TRIGGERS = ["!quote"]
DESCRIPTION = "Random quote from the quotes.txt pool"
COOLDOWN_DEFAULT = 30
ALLOWED_CHANNELS = ["#quotes", "#bot"] # set to None for all channels
ALLOW_DM = True

_QUOTES_FILE = os.path.join(os.path.dirname(__file__), "quotes.txt")


def _load_quotes():
    try:
        with open(_QUOTES_FILE, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


async def handle(ctx):
    quotes = _load_quotes()
    if not quotes:
        return "No quotes configured — add lines to commands/quotes.txt"
    return random.choice(quotes)
