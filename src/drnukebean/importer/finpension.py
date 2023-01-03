"""
This is a beancount importer for Finpension. 
Setup:
1) have a running beancount system
2) run 'bean-extract config.py path/to/finpension/transaction_report.csv -f mainLedgerFile.bean
"""

import pandas as pd
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import warnings
import pickle
import re
import numpy as np
import sys
from loguru import logger

import yaml
from os import path

from beancount.query import query
from beancount.parser import options
from beancount.ingest import importer
from beancount.core import data, amount
from beancount.core.number import D
from beancount.core.number import Decimal
from beancount.core import position
from beancount.core.number import MISSING

# some constants set by FinPension in the csv export header
FP_currency = 'Asset Currency'
FP_proceeds = 'Cash Flow'
FP_asseet_price = 'Asset Price in CHF'


class FinPensionImporter(importer.ImporterProtocol):
    """
    Beancount Importer for Finpension
    """

    def __init__(self,
                 deposit_account = None,
                 root_account = None,
                 isin_lookup = None, # for example Assets:Invest:IB
                 div_suffix='Div',  # suffix for dividend Account , like Assets:Invest:IB:VT:Div
                 interest_suffix='Interest',
                 fees_suffix='Fees',
                 pnl_suffix='PnL',
                 file_encoding="utf-8-sig",
                 sep=";",
                 # a regex pattern that allows to distinguish between pillar 2&3 and individual portfolios
                 regex=r"finpension_(S[2,3][a]?)_(Portfolio\d)",
                 ):

        self.root_account = root_account  # root account from  which others can be derived
        self.deposit_account = deposit_account
        self.div_suffix = div_suffix
        self.interest_suffix = interest_suffix
        self.fees_suffix = fees_suffix
        self.pnl_suffix = pnl_suffix
        self.isin_lookup = isin_lookup
        self.file_encoding = file_encoding
        self.flag = '*'
        self.regex = regex
        self.sep = sep

    def identify(self, file):
        # intended file format is *finpension_s2_p1* for säule(pillar) 2 portfolio 1
        result = bool(re.search(self.regex, file.name, re.IGNORECASE))
        logger.info(
            f"identify assertion for finpension importer and file '{file.name}': {result}")
        return result

    def getLiquidityAccount(self, currency):
        return ':'.join([self.main_account, currency])

    def getDivIncomeAcconut(self, currency, symbol):
        return ':'.join([self.main_account.replace('Assets', 'Income'), symbol, self.div_suffix])

    def getInterestIncomeAcconut(self, currency):
        return ':'.join([self.main_account.replace('Assets', 'Income'), self.interest_suffix, currency])

    def getAssetAccount(self, symbol):
        return ':'.join([self.main_account, symbol])

    def getFeesAccount(self, currency):
        return ':'.join([self.main_account.replace('Assets', 'Expenses'), self.fees_suffix, currency])

    def file_account(self, _):
        return self.main_account or self.root_account

    def fix_accounts(self, file):
        try:
            pillar, portfolio = re.search(
                self.regex, file.name, re.IGNORECASE).groups()
        except AttributeError as e:
            logger.error(
                f"could not extract pillar and/or portfolio from filename {file.name} with regex pattern {self.regex}.")
            raise AttributeError(e)
        new_account = re.sub(r"S[2,3]a?", pillar, self.root_account)
        self.main_account = re.sub(r"Portfolio\d", portfolio, new_account)    
            


    def extract(self, file_, existing_entries=None):
        # the actual processing of the csv export

        # fix Account names with regard to pillar 2/3 and different portfolios.
        self.fix_accounts(file_)

        df = pd.read_csv(file_.name,
                         sep=self.sep,
                         )
        # convert specific columns to Decimal with specific precisions
        to_decimal_dict = {"Number of Shares": 3,
                           "Asset Price in CHF": 2,
                           "Cash Flow": 2,
                           "Balance": 2}
        for col, digits in to_decimal_dict.items():
            df[col] = df[col].apply(lambda x: Decimal(x).__round__(digits))

        df['Date'] = pd.to_datetime(df['Date']).apply(datetime.date)

        # disect the complete report in similar transactions
        # abit messy since Finpension uses different tags in pillar 2/3a
        trades = df[df.Category.isin(["Portfolio Transaction",'Buy','Sell'])]
        deposits = df[df.Category.isin(["Transfer vested benefits",'Deposit'])]
        fees = df[df.Category.isin(["Implementation fees",'Flat-rate administrative fee'])]
        interests = df[df.Category == "Interests"]
        dividends = df[df.Category.isin(["Dividend and Interest Distributions",'Dividend'])]

        return_txn = (self.Trades(trades)
                      + self.Deposits(deposits) # omitted for only occuring very rarely
                      + self.Fees(fees)
                      + self.Interest(interests)
                      + self.Dividends(dividends)
                      + self.Balances(df)
                      )

        return return_txn

    def Trades(self, trades):
        bean_transactions = []
        for idx, row in trades.iterrows():
            currency = row[FP_currency]
            isin = row['ISIN']
            symbol = self.isin_lookup.get(isin)
            asset = row['Asset Name']
            if symbol is None:
                logger.error(
                    f"Could not fetch isin {row['symbol']} from supplied ISINs {list(self.isin_lookup.keys())}")
                continue
            proceeds = amount.Amount(row[FP_proceeds], currency)

            quantity = amount.Amount(row['Number of Shares'], symbol)
            price = amount.Amount(row[FP_asseet_price], "CHF")

            postings = [
                data.Posting(self.getAssetAccount(symbol),
                             quantity, None, price, None, None),
                data.Posting(self.getLiquidityAccount(currency),
                             proceeds, None, None, None, None),
            ]
            if quantity.number > 0:
                buy_sell = "BUY"
            else:
                buy_sell = "SELL"
            bean_transactions.append(
                data.Transaction(data.new_metadata('Buy', 0),
                                 row['Date'],
                                 self.flag,
                                 isin,     # payee
                                 ' '.join(
                                     [buy_sell, quantity.to_string(), '@', price.to_string()+";", asset]),
                                 data.EMPTY_SET,
                                 data.EMPTY_SET,
                                 postings
                                 ))
        return bean_transactions

    def Fees(self, fees):

        bean_transactions = []
        for idx, row in fees.iterrows():
            currency = row[FP_currency]
            amount_ = amount.Amount(row[FP_proceeds], currency)

            # make the postings, two for fees
            postings = [data.Posting(self.getFeesAccount(currency),
                                     -amount_, None, None, None, None),
                        data.Posting(self.getLiquidityAccount(currency),
                                     amount_, None, None, None, None)]
            meta = data.new_metadata(__file__, 0, {})  # actually no metadata
            bean_transactions.append(
                data.Transaction(meta,
                                 row['Date'],
                                 self.flag,
                                 'FinPension',     # payee
                                 "Fees",
                                 data.EMPTY_SET,
                                 data.EMPTY_SET,
                                 postings))
        return bean_transactions

    def Dividends(self, dividends):
        # this function crates Dividend transactions from IBKR data
        # make dividend & WHT transactions

        bean_transactions = []
        for idx, row in dividends.iterrows():
            currency = row[FP_currency]
            isin = row['ISIN']
            symbol = self.isin_lookup.get(isin)
            if symbol is None:
                logger.error(
                    f"Could not fetch isin {row['symbol']} from supplied ISINs {list(self.isin_lookup.keys())}")
                continue
            amount_div = amount.Amount(row[FP_proceeds], currency)
            
            

            postings = [data.Posting(self.getDivIncomeAcconut(currency, symbol),
                                     -amount_div, None, None, None, None),
                        data.Posting(self.getLiquidityAccount(currency),
                                     amount_div, None, None, None, None)
                        ]

            metadict = {'isin': isin}
            per_share_number = row[FP_asseet_price]
            if not per_share_number.is_nan():
                pershare = amount.Amount(row[FP_asseet_price], currency)
                metadict.update({'per_share': pershare})

            meta = data.new_metadata(
                'dividend', 0, metadict)
            bean_transactions.append(
                data.Transaction(meta,  # could add div per share, ISIN,....
                                 row['Date'],
                                 self.flag,
                                 isin,     # payee
                                 f"Dividend {symbol}; {row['Asset Name']}",
                                 data.EMPTY_SET,
                                 data.EMPTY_SET,
                                 postings
                                 ))

        return bean_transactions

    def Interest(self, int_):
        # calculates interest payments from IBKR data
        bean_transactions = []
        for idx, row in int_.iterrows():
            currency = row[FP_currency]
            amount_ = amount.Amount(row[FP_proceeds], currency)

            # make the postings, two for interest payments
            # received and paid interests are booked on the same account
            postings = [data.Posting(self.getInterestIncomeAcconut(currency),
                                     -amount_, None, None, None, None),
                        data.Posting(self.getLiquidityAccount(currency),
                                     amount_, None, None, None, None)
                        ]
            meta = data.new_metadata('Interest', 0)
            bean_transactions.append(
                data.Transaction(meta,  # could add div per share, ISIN,....
                                 row['Date'],
                                 self.flag,
                                 'FinPension',     # payee
                                 "Interest",
                                 data.EMPTY_SET,
                                 data.EMPTY_SET,
                                 postings
                                 ))
        return bean_transactions

    def Balances(self, df):
        # generate Balance statements for every latest transaction
        # (there may be multiple, no idea how to pick the right one)
        # simply make a balance for all values, the correct one should be one of them

        bean_transactions = []
        df = df[df['Date'] == df['Date'].max()]
        for idx, row in df.iterrows():
            currency = row[FP_currency]
            amount_ = amount.Amount(row['Balance'], currency)
            meta = data.new_metadata('balance', 0)
            bean_transactions.append(data.Balance(
                meta,
                row['Date'] + timedelta(days=1),  # see tariochtools EC imp.
                self.getLiquidityAccount(currency),
                amount_,
                None,
                None))
        return bean_transactions

    def Deposits(self, dep):
            bean_transactions = []
            if len(self.deposit_account) == 0:  # control this from the config file
                return []
            for idx, row in dep.iterrows():
                currency = row[FP_currency]
                amount_ = amount.Amount(row[FP_proceeds], currency)

                # make the postings. two for deposits
                postings = [data.Posting(self.deposit_account,
                                        -amount_, None, None, None, None),
                            data.Posting(self.getLiquidityAccount(currency),
                                        amount_, None, None, None, None)
                            ]
                meta = data.new_metadata('deposit/withdrawel', 0)
                bean_transactions.append(
                    data.Transaction(meta,  # could add div per share, ISIN,....
                                    row['Date'],
                                    self.flag,
                                    'self',     # payee
                                    "deposit / withdrawal",
                                    data.EMPTY_SET,
                                    data.EMPTY_SET,
                                    postings
                                    ))
            return bean_transactions