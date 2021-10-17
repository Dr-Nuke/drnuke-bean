#! python
import re

# a collection of commonly used functions

def remove_spaces(s):
    # removes leading and trailing spaces, and collapses multiple space
    # characters into one space character. 
    # i.e. " Hello   this is      my   ledger  " -> "Hello this is my ledger"
    # used to combat bloated bank statement strings (payyee & narration)
    return re.sub(' +', ' ', s.strip())