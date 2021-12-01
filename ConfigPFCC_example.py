# This is an example Config file for the Beancount Postfinance importer from Dr-Nuke
# first we define some manual modifications to the individual transactions as 
# a function that is later passed to the beancount parser.
# Then we make the importer Instance. Enter your CC number and details there.
# In this example, we also use the smart importer from tariochtools

from smart_importer import apply_hooks, PredictPayees, PredictPostings
from beancount.ingest import extract
from drnukebean.importer import PFCC
from drnukebean.importer.PFG import PFGImporter
from beancount.core.number import Decimal
from beancount.core import data
from beancount.core.amount import Amount
import re


def automatic_fixes(d):
    """
    This function allows the user to specify multiple automatic manipulations of the 
    ingested bank statement data. Its return will be the input for the
    data.Transaction() object to be created from that line of the statement
    d: dict() with keys of a future statement, i.e. 'narration', 'payee',...

    If you don't want any modifications, simply return d.

    Consider these existing modification as working examples
    """

    monthstring=d['date'].strftime('%b %Y') 

    # make default posting# make statement
    d['postings']=[data.Posting(d['account'],
            d['amount'],
            None,
            None,
            None,
            None)]

    # SBB
    if bool(re.search('SBB', d['narration'], re.IGNORECASE)):
        d['narration']='Bahnticket'
        d['payee']='SBB'
        d['flag']='!' # add individual metadata

    # ZVV
    if bool(re.search('ZVV', d['narration'], re.IGNORECASE)):
        d['narration']='Bilet'
        d['payee']='ZVV'
        d['flag']='!' # add individual metadata   
    
    # coop
    if bool(re.search('coop', d['narration'], re.IGNORECASE)):
        d['narration']='Food'
        d['payee']='Coop'
        
    # migros    
    if bool(re.search('Migros', d['narration'], re.IGNORECASE)):
        d['narration']='Food'
        d['payee']='Migros'
        
    return d

    
extract.HEADER = ''
smart=False # make false to remove smart -importer


PFCC_ = PFCC.PFCCImporter(
    '6393', # last 4 digits of CC number
    'Assets:Liq:PF:Kreditkarte',
    currency='CHF',
    file_encoding='ISO-8859-1',
    manual_fixes = automatic_fixes,
    filetypes = ['.csv'] # optional; empty list will allow all filetypes
    )
if smart: apply_hooks(PFCC_, [PredictPostings()])


CONFIG = [PFCC_]
