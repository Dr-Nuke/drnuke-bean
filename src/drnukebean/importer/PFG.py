# beancount importer for Postfinance.
import csv
import re
from datetime import datetime, timedelta

from beancount.core import data
from beancount.core.amount import Amount
from beancount.ingest import importer
from beancount.core.number import Decimal
from . import util as u

class InvalidFormatError(Exception):
    pass

def fmt_number_de(value: str) -> Decimal:
    #a fix for region specific number formats
    thousands_sep = '.'
    decimal_sep = ','

    return Decimal(value.replace(thousands_sep, '').replace(decimal_sep, '.'))


def DecimalOrZero(value):
    # for string to number conversion with empty strings
    try:
        return Decimal(value)
    except:
        return Decimal(0.0)
        
class PFGImporter(importer.ImporterProtocol):
    """
    Beancount Importer for the Postfinance giro account bank statements
    """
    def __init__(self, iban, account, currency='EUR', file_encoding='utf-8',manualFixes={}):

        self.account = account
        self.currency = currency
        self.file_encoding = file_encoding
        self.language=''
        self.iban=iban.replace(' ','')

        self._date_from = None
        self._date_to = None
        self._balance_amount = None
        self._balance_date = None
        self.delimiter=';'
        self.manualFixes=manualFixes
        

    def name(self):
        return 'PFG {}'.format(self.__class__.__name__)

    def file_account(self, _):
        return self.account

    def file_date(self, file_):
        self.extract(file_)

        return self._date_from

    def identify(self, file_):
        return self.checkForAccount(file_)
    
    def checkForAccount(self,file_):
        # find amatch between the config's IBAN and the parsed file's iban
        try:
            f = open(file_.name, encoding=self.file_encoding)
        except IOError:
            print('Cannot open/read {}'.format(file_.name))
            return False
        with f as fd:
            reader = csv.reader(fd, delimiter=self.delimiter)
            L=[3] # row index in which iban is found
            C=1 # column index in which iban is found
            for i, line in enumerate(reader):
                if i in L:   
                    try: 
                        return line[C]==self.iban
                    except IndexError:
                        return False
    
    def getLanguage(self,file_):
        # find out which language the report is in based on the first line
        langdict={'Datum von:':'DE',
                  'Date from:':'EN'}
        try:
            with open(file_.name, encoding=self.file_encoding) as f:
                line = f.readline()
                for key,val in langdict.items():
                    if line.startswith(key):
                        return val
                    
        except:
            print('Cannot determine language of {}'.format(file_.name))
            pass
        return None
   
                    
    def extract(self, file_,existing_entries=None):
        # the actual text processing of the bank statement
        
        self.language=self.getLanguage(file_)
        if self.language==None:
            return []
        entries = []
       
        if not self.checkForAccount(file_):
            raise InvalidFormatError()

        with open(file_.name, encoding=self.file_encoding) as fd:

            reader = csv.reader(fd, delimiter= self.delimiter)

            line=next(reader) # from date
            self._date_from = datetime.strptime(line[1], '%Y-%m-%d').date()
            line=next(reader) # to date
            self._date_to = datetime.strptime(line[1], '%Y-%m-%d').date()

            line=next(reader) # ignore booking type line
            line=next(reader) # ignoring IBAN line
            line=next(reader) # check currency
            if line[1]!=self.currency:
                print('Importer vs. bankstatement currency: {} {} in {}'.format(self.currency,line[1],file_.name))
                return []

            cols=next(reader) # get the headers fot the actual transaction table
            # headers for english files:
            # 0 :  Booking date
            # 1 :  Notification text
            # 2 :  Credit in CHF
            # 3 :  Debit in CHF
            # 4 :  Value
            # 5 :  Balance in CHF

            # Data entries
            for i,row in enumerate(reader):
                meta = data.new_metadata(file_.name, i)
                if len(row)==0: # "end" of bank statment
                    break
                credit=DecimalOrZero(row[2])
                debit=DecimalOrZero(row[3]) 
                total=credit+debit # mind PF sign convention
                date= datetime.strptime(row[0],'%Y-%m-%d').date()
                amount = Amount(total, self.currency)
                balance=Amount(DecimalOrZero(row[5]), self.currency)
                description = row[1]
                # pdb.set_trace()
                # get closing balance, if available
                if (i==0) & (row[5]!='') : 
                    entries.append(
                        data.Balance(
                            meta,
                            date + timedelta(days=1), # see tariochtools EC imp.
                            self.account,
                            balance,
                            None,
                            None))
                    
                # make statement
                entries.append(ManualFixes(account=self.account,
                            amount=amount,
                           meta=meta,
                           date=date,
                           flag=self.FLAG,
                           payee='',
                           narration=description))
                
        return entries

def ManualFixes(account,
                amount,
                meta,
                date,
                flag,
                payee,
                narration):
    # manually fix some common transactions
    postings=[data.Posting(account,
                         amount,
                         None,
                         None,
                         None,
                         None)]
    
#     fixes=['coop':{'narration':'Food','payee':'Coop'},
#            'migros':{'narration':'Food','payee':'migros'},
#           'KONTOFÜHRUNG':{'narration':'Kontogebühr ','payee':'PostFinance'},
#           'BONUS POSTFINANCE':{'narration':'Kreditkartenrechnung  ','payee':'self'},
#           'DD-BASISLASTSCHRIFT':{'narration':'Kreditkartenrechnung  ','payee':'self'},
#           'DD-BASISLASTSCHRIFT':{'narration':'Kreditkartenrechnung  ','payee':'self'},
#           'DD-BASISLASTSCHRIFT':{'narration':'Kreditkartenrechnung  ','payee':'self'},
#           'DD-BASISLASTSCHRIFT':{'narration':'Kreditkartenrechnung  ','payee':'self'},
#           'DD-BASISLASTSCHRIFT':{'narration':'Kreditkartenrechnung  ','payee':'self'}]
    
    # general shortign
    general='KAUF/DIENSTLEISTUNG VOM \d\d.\d\d.\d{4} KARTEN NR. \w{8} '
    if bool(re.search(general, narration, re.IGNORECASE)):
        narration=re.sub(general,'',narration)
    
    # coop
    if bool(re.search('coop', narration, re.IGNORECASE)):
        narration='Food'
        payee='Coop'
        
    # migros    
    if bool(re.search('Migros', narration, re.IGNORECASE)):
        narration='Food'
        payee='Migros'
        
     # Giro    
    if bool(re.search('KONTOFÜHRUNG', narration, re.IGNORECASE)):
        narration='Kontogebühr'
        payee='PostFinance'   
        
    # CC bill    
    if bool(re.search('DD-BASISLASTSCHRIFT', narration, re.IGNORECASE)):
        narration='Kreditkartenrechnung'
        payee='self'      
        
    # BARGELDBEZUG 
    if bool(re.search('BARGELDBEZUG ', narration, re.IGNORECASE)):
        narration='abheben'
        payee='self'  
        
    # SwissMobility 
    if bool(re.search('SwissMobility', narration, re.IGNORECASE)):
        narration='SwissMobility'
        payee='SwissMobility'    
        
    # KV
    if bool(re.search('Assura', narration, re.IGNORECASE)):
        narration='Krankenversicherung'
        payee='Assura' 
        flag='!'
        
    # Thomann
    if bool(re.search('Thomann', narration, re.IGNORECASE)):
        narration='Thomann'
        payee='Thomann'
        flag='!'
    
    #les framboises
    if bool(re.search('les framboises', narration, re.IGNORECASE)):
        narration='Framboises'
        payee='Framboises'
        flag='!'
    
        
        
        
        
    return data.Transaction(meta,
                            date,
                            flag,
                            payee,
                            narration,
                            data.EMPTY_SET,
                            data.EMPTY_SET,
                            postings
                            )

    
    