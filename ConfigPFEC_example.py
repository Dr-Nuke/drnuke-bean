# This is an example Config file for the Beancount Postfinance importer from Dr-Nuke
# first we define some manual modifications to the individual transactions as 
# a function that is later passed to the beancount parser.
# Then we make the importer Instance. Enter your IBAN and details there.
# In this example, we also use the smart importer from tariochtools

from smart_importer import apply_hooks, PredictPayees, PredictPostings
from beancount.ingest import extract
from drnukebean.importer.PFG import PFGImporter
from beancount.core.number import Decimal
from beancount.core import data
from beancount.core.amount import Amount
import re


extract.HEADER = '' # reduce unneccessary printout spam
smart=False # make false to remove smart -importer

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

    # make default posting# 
    d['postings']=[data.Posting(d['account'],
            d['amount'],
            None,
            None,
            None,
            None)]

    # general shorting of Postfinance standard phrase
    general='KAUF/DIENSTLEISTUNG VOM \d\d.\d\d.\d{4} KARTEN NR. \w{8} '
    if bool(re.search(general, d['narration'], re.IGNORECASE)):
        d['narration']=re.sub(general,'',d['narration'])
    
    # coop
    if bool(re.search('coop', d['narration'], re.IGNORECASE)):

        #Bau & Hobby
        if bool(re.search('b+h', d['narration'], re.IGNORECASE)):
            d['narration']=''
            d['payee']='Coop Bau & Hobby'
            d['flag']='!'

        # Coop gas station
        elif bool(re.search('MINERALOEL', d['narration'], re.IGNORECASE)):
            
            d['payee']='Coop Mineraloel'
            d['flag']='!'

            # coop
            re_Waren=r'(?i)Waren (\S+)'
            amount_waren=re.search(re_Waren,d['narration'])
            if amount_waren: 
                amount_waren=Amount(Decimal( amount_waren.groups()[0]), 'CHF')
                d['postings'].extend([
                    data.Posting('Expenses:Other', amount_waren,None,None,None,None),
                ])
                d['flag']='!'

            re_Benzin=r'(?i)Treibstoff (\S+)'
            amount_sprit=re.search(re_Benzin,d['narration'])
            if amount_sprit: # sprit
                d=add_gas_purchase(d,
                                    'Expenses:Gasoline',
                                    -Amount(Decimal( amount_sprit.groups()[0]), 'CHF'))
                                
        # otherwise, its just coop supermarket
        else:    
            d['narration']='Food'
            d['payee']='Coop'
        
    # migros    
    elif bool(re.search('Migros', d['narration'], re.IGNORECASE)):
        d['narration']='Food'
        d['payee']='Migros'
        
     # Giro fees   
    elif bool(re.search('KONTOFÜHRUNG', d['narration'], re.IGNORECASE)):
        d['narration']='Kontogebühr'
        d['payee']='PostFinance'   
        
    # CC bill    
    elif bool(re.search('DD-BASISLASTSCHRIFT', d['narration'], re.IGNORECASE)):
        d['narration']='Kreditkartenrechnung '+monthstring
        d['payee']='self'      

    # Salary    
    elif bool(re.search('MyCompany Salary', d['narration'], re.IGNORECASE)):
        d['narration']='Salary '+monthstring
        d['payee']='MyCompany'
        d['postings']=[
            data.Posting('Income:MyCompany:Salary',            Amount(Decimal('-5154.12'), 'CHF'),None,None,None,None),
            data.Posting('Income:MyCompany:Expenses',          Amount(Decimal(  '-61.80'), 'CHF'),None,None,None,None),
            data.Posting('Income:MyCompany:S2EmployerContrib', Amount(Decimal( '-199.24'), 'CHF'),None,None,None,None),
            data.Posting('Expenses:LabourCosts:AHV',           Amount(Decimal(  '271.89'), 'CHF'),None,None,None,None),
            data.Posting('Expenses:LabourCosts:NBU',           Amount(Decimal(   '56.70'), 'CHF'),None,None,None,None),
            data.Posting('Expenses:Taxes:SourceTax',           Amount(Decimal(  '496.56'), 'CHF'),None,None,None,None),
            data.Posting('Assets:Bank:Giro:Checking',          Amount(Decimal( '4221.06'), 'CHF'),None,None,None,None),
            data.Posting('Assets:Invest:S2:MyCompany:CHF',     Amount(Decimal(  '169.70'), 'CHF'),None,None,None,None),
            data.Posting('Assets:Invest:S2:MyCompany:CHF',     Amount(Decimal(  '199.24'), 'CHF'),None,None,None,None),
            ]

    #Rent
    elif (bool(re.search('Landlord', d['narration'], re.IGNORECASE)) and  (d['amount'].number == Decimal(-1200))):
        d['narration']='Rent ' +monthstring
        d['payee']='Landlord' 
    
    
    # Cash 
    elif bool(re.search('BARGELDBEZUG ', d['narration'], re.IGNORECASE)):
        d['narration']='Cash withdrawl'
        d['payee']='self'  


    return d

def add_gas_purchase(d,account,amount):
    # adds the gasoline specific meta
    d['postings'].extend([
        data.Posting(account, -amount,None,None,None,None),
    ])
    d['meta']['liter']=''
    d['meta']['kilometer']=''
    d['payee']=d['narration']
    d['narration']='Gasoline'
    d['flag']='!'
    return d

PFEC_ = PFGImporter(
    iban = 'CH94 0123 4567 8910 1112 0',
    account = 'Assets:Bank:Checking',
    currency = 'CHF',
    file_encoding = 'ISO-8859-1',
    manual_fixes = automatic_fixes,
    filetypes = ['.csv'] # optional; empty list will allow all filetypes
    )
if smart: apply_hooks(PFEC_, [PredictPostings()])

CONFIG = [PFEC_]

