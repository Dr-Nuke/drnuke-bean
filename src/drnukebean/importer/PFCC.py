# beancount importer for Postfinance credit card.
# see the importer for the checkings account for more detailed documentation
import csv
import re
from datetime import datetime, timedelta


from beancount.core import data
from beancount.core.amount import Amount
from beancount.ingest import importer
from beancount.core.number import Decimal
from .util import remove_spaces
from pathlib import Path

import pdb


class InvalidFormatError(Exception):
    pass


def fmt_number_de(value: str) -> Decimal:
    thousands_sep = '.'
    decimal_sep = ','

    return Decimal(value.replace(thousands_sep, '').replace(decimal_sep, '.'))


def DecimalOrZero(value):
    # for string to number conversion with empty strings
    try:
        return Decimal(value)
    except:
        return Decimal(0.0)


class PFCCImporter(importer.ImporterProtocol):
    def __init__(self,
                 ccnumber,
                 account,
                 currency='EUR',
                 file_encoding='utf-8',
                 manual_fixes=0,
                 filetypes=[]):

        self.account = account
        self.currency = currency
        self.file_encoding = file_encoding
        self.language = ''
        self.ccnumber = ccnumber.replace(' ', '')[-4:]
        self.filetypes = [typ.lower() for typ in filetypes]

        self._date_from = None
        self._date_to = None
        self._balance_amount = None
        self._balance_date = None
        self.delimiter = ';'
        self.manual_fixes = manual_fixes

        self.tags = {'Saldovortrag': {'EN': 'Balance brought forward',
                                      'DE': 'Saldovortrag'}}

    def name(self):
        return 'PFCC {}'.format(self.__class__.__name__)

    def file_account(self, _):
        return self.account

    def file_date(self, file_):
        self.extract(file_)

        return self._date_from

    def identify(self, file_):
        return self.checkForAccount(file_)

    def checkForAccount(self, file_):
        # find amatch between the config's cc number and the parsed file's one

        # first check if the file is of a desired file type. this prevents searching
        # pdf's and jpg's in case we just want csv's.
        if (len(self.filetypes) > 0) and (not (Path(file_.name).suffix.lower() in self.filetypes)):
            return False

        try:
            f = open(file_.name, encoding=self.file_encoding)
        except IOError:
            print('Cannot open/read {}'.format(file_.name))
            return False
        with f as fd:
            reader = csv.reader(fd, delimiter=self.delimiter)
            L = [1]  # row index in which cc number is found
            C = 1  # column index in which iban is found
            for i, line in enumerate(reader):
                if i in L:
                    try:
                        return self.ccnumber in line[C]
                    except IndexError:
                        return False

    def getLanguage(self, file_):
        # find out which language the report is in based on the first line
        langdict = {'Kartenkonto:': 'DE',
                    'Card account:': 'EN'}
        try:
            with open(file_.name, encoding=self.file_encoding) as f:
                line = f.readline()
                for key, val in langdict.items():
                    if line.startswith(key):
                        return val

        except:
            pass
        print('Cannot determine language of {}'.format(file_.name))
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

            line = next(reader)  # account info, ignore
            line = next(reader)  # card info, ignore
            line = next(reader)  # date info
            dates = line[1].split(' - ')

            self._date_from = datetime.strptime(dates[0], '%d.%m.%Y').date()
            self._date_to = datetime.strptime(dates[1], '%d.%m.%Y').date()

            # get the headers fot the actual transaction table
            cols = next(reader)
            # headers for german files:
            # 0 Datum
            # 1 Bezeichnung
            # 2 Gutschrift in CHF
            # 3 Lastschrift in CHF
            # 4 Betrag in CHF

            if cols[4][-3:] != self.currency:
                print('Importer vs. bankstatement currency: {} {} in {}'.format(
                    self.currency, line[4][-3:], file_.name))
                return []

            # Data entries
            for i, row in enumerate(reader):
                if len(row) == 0:  # "end" of bank statment
                    break
                if row[1] == 'Total':  # ignore this entry
                    continue
                # skip credit card bill or charge transaction, as they already appear on the giro account
                if ('CH-DD ZAHLUNG' in row[1]) or ('ONLINE LADUNG KARTENKONTO' in row[1]):
                    continue

                meta = data.new_metadata(file_.name, i)
                credit = DecimalOrZero(row[2])
                debit = DecimalOrZero(row[3])
                total = credit-debit  # mind PF sign convention
                date = datetime.strptime(row[0], '%Y-%m-%d').date()
                amount = Amount(total, self.currency)

                description = row[1]
                # pdb.set_trace()
                # get closing balance, if available
                if (row[1] == self.tags['Saldovortrag'][self.language]):
                    balance = Amount(-DecimalOrZero(row[3]), self.currency)
                    entries.append(
                        data.Balance(
                            meta,
                            # see tariochtools EC imp.
                            date + timedelta(days=1),
                            self.account,
                            balance,
                            None,
                            None))
                else:    # if not balance, it's a transaction
                    # prepare/ make statement
                    d = dict(amount=amount,
                             account=self.account,
                             meta=meta,
                             flag=self.FLAG,
                             narration=description,
                             payee='',
                             date=date,
                             )

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
