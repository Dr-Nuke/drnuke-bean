# beancount importer for Postfinance.
import csv
from pathlib import Path
import re
from datetime import datetime, timedelta
import logging

from beancount.core import data
from beancount.core.amount import Amount
from beancount.ingest import importer
from beancount.core.number import Decimal
from .util import remove_spaces


class InvalidFormatError(Exception):
    pass


def fmt_number_de(value: str) -> Decimal:
    # a fix for region specific number formats
    thousands_sep = '.'
    decimal_sep = ','

    return Decimal(value.replace(thousands_sep, '').replace(decimal_sep, '.'))


def DecimalOrZero(value):
    # for string to number conversion with empty strings
    if not value:
        return Decimal('0.00')
    try:
        value_no_thousands = value.replace("'","")
        return Decimal(f'{float(value_no_thousands):.2f}')
    except:
        return Decimal('0.00')


def strip_new_pf_format(s):
    return s.strip("=").strip('"')


class PFGImporter(importer.ImporterProtocol):
    """
    Beancount Importer for the Postfinance giro account bank statements
    """

    def __init__(self,
                 iban,
                 account,
                 balance_account=None,
                 currency='EUR',
                 file_encoding='utf-8',
                 manual_fixes=None,
                 filetypes=[],
                 date_format='%d.%m.%Y'):

        self.account = account
        if balance_account is not None:
            self.balance_account = balance_account
        else:
            self.balance_account = account
        self.currency = currency
        self.file_encoding = file_encoding
        self.language = ''
        self.iban = iban.replace(' ', '')
        self.filetypes = [typ.lower() for typ in filetypes]

        self._date_from = None
        self._date_to = None
        self._balance_amount = None
        self._balance_date = None
        self.delimiter = ';'
        self.manual_fixes = manual_fixes
        self.date_format = date_format

    def name(self):
        return 'PFG {}'.format(self.__class__.__name__)

    def file_account(self, _):
        return self.account

    def file_date(self, file_):
        self.extract(file_)
        return self._date_from

    def identify(self, file_):
        check = self.checkForAccount(file_)
        logging.info(f"identify PFG importer with file {file_.name}: {check}")
        return check

    def checkForAccount(self, file_):
        # find amatch between the config's IBAN and the parsed file's iban
        # first check if the file is of a desired file type. this prevents searching
        # pdf's and jpg's in case we just want csv's.
        if (len(self.filetypes) > 0) and (not (Path(file_.name).suffix.lower() in self.filetypes)):
            return False

        try:
            f = open(file_.name, encoding=self.file_encoding)

            with f as fd:
                reader = csv.reader(fd, delimiter=self.delimiter)
                L = [1,3]  # row index in which iban is found
                C = 1  # column index in which iban is found
                for i, line in enumerate(reader):
                    if i in L:
                        try:
                            if line[C] == self.iban:
                                return True
                        except IndexError:
                            return False
                return False

        except (UnicodeDecodeError, IOError) as e:
            if isinstance(e, UnicodeDecodeError):
                print(
                    f'***** file {file_.name} in PFGImporter throws UnicodeDecodeError for encoding {e.encoding} of byte {e.object[e.start:e.end]} at position {e.start} and reason {e.reason}')
            elif isinstance(e, IOError):
                print('***** Cannot open/read {}'.format(file_.name))
            return False

    def getLanguage(self, file_):
        # find out which language the report is in based on the first line
        langdict = {'Datum von:': 'DE',
                    'Date from:': 'EN',
                    'Buchungsart': 'DE'}
        try:
            with open(file_.name, encoding=self.file_encoding) as f:
                line = f.readline()
                for key, val in langdict.items():
                    if key in line:
                        return val

        except:
            print('***** Cannot determine language of {}'.format(file_.name))
            pass
        print(
            f'***** None of the language detection strings {list(langdict.keys())} found in line "{line}"')
        return None

    def extract(self, file_, existing_entries=None):
        # the actual text processing of the bank statement

        self.language = self.getLanguage(file_)
        if self.language == None:
            return []
        entries = []

        if not self.checkForAccount(file_):
            raise InvalidFormatError()

        with open(file_.name, encoding=self.file_encoding) as fd:

            reader = csv.reader(fd, delimiter=self.delimiter)

            line = next(reader)  # from date
            self._date_from = datetime.strptime(
                strip_new_pf_format(line[1]), self.date_format).date()
            line = next(reader)   # to date
            self._date_to = datetime.strptime(
                strip_new_pf_format(line[1]), self.date_format).date()

            line = next(reader)  # ignore booking type line
            line = next(reader)  # ignoring IBAN line
            line = next(reader)    # check currency
            if strip_new_pf_format(line[1]) != self.currency:
                print('Importer vs. bankstatement currency: {} {} in {}'.format(
                    self.currency, line[1], file_.name))
                return []
            line = next(reader)  # ignore empty line
            # get the headers fot the actual transaction table
            cols = next(reader)
            # headers for english files:
            # 0 :  Booking date
            # 1 :  Notification text
            # 2 :  Credit in CHF
            # 3 :  Debit in CHF
            # 4 :  Value
            # 5 :  Balance in CHF

            first_transaction = True  # the first tx in the csv is the latest
            # Data entries
            for i, row in enumerate(reader):
                if len(row) < 5:  # "end" of bank statment or empty line
                    continue
                meta = data.new_metadata(file_.name, i)
                credit = DecimalOrZero(row[2])
                debit = DecimalOrZero(row[3])
                total = credit+debit  # mind PF sign convention
                date = datetime.strptime(row[0], self.date_format).date()
                amount = Amount(total, self.currency)
                description = row[1]
                # get closing balance, if available
                # i just happens that the first trasaction contains the latest balance
                if (first_transaction == True) & (len(row)==8):
                    balance = Amount(DecimalOrZero(row[5]), self.currency)
                    entries.append(
                        data.Balance(
                            meta,
                            # see tariochtools EC imp.
                            date + timedelta(days=1),
                            self.balance_account,
                            balance,
                            None,
                            None))
                    first_transaction = False

                # prepare/ make the transaction
                d = dict(amount=amount,
                         account=self.account,
                         meta=meta,
                         flag=self.FLAG,
                         narration=description,
                         payee='',
                         date=date,
                         postings=[data.Posting(self.account,
                                                amount,
                                                None,
                                                None,
                                                None,
                                                None)]
                         )

                if self.manual_fixes is not None:
                    d = self.manual_fixes(d)

                trans = data.Transaction(d['meta'],
                                         d['date'],
                                         d['flag'],
                                         remove_spaces(d['payee']),
                                         remove_spaces(d['narration']),
                                         data.EMPTY_SET,
                                         data.EMPTY_SET,
                                         d['postings']
                                         )
                entries.append(trans)
        return entries
