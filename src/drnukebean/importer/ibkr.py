"""
This is a beancount importer for Interactive Brokers. 
Setup:
1) have a running beancount system
2) activate IB FLexQuery with the entries specified in []
3) in the config.py file, specify a file location wiht your IBKR FlexQuery 
    Credentials
4) run 'bean-extract config.py ibkr.yml -f mainLedgerFile.bean
"""

import pandas as pd
from datetime import datetime, timedelta
import xml.etree.ElementTree as ET
import warnings
import pickle
import re

import yaml
from os import path
from ibflex import client, parser, Types
from ibflex.enums import CashAction

from beancount.query import query
from beancount.parser import options
from beancount.ingest import importer
from beancount.core import data, amount
from beancount.core.number import D
from beancount.core.number import Decimal

from tariochbctools.importers.general.priceLookup import PriceLookup

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
        
def AmountAdd(A1,A2):
    # add two amounts 
    if A1.currency == A2.currency:
        quant=A1.number+A2.number
        return amount.Amount(quant,A1.currency)
    else:
        raise('Cannot add amounts of differnent currencies: {} and {}'.format(
            A1.currency,A1.currency))


class IBKRImporter(importer.ImporterProtocol):
    """
    Beancount Importer for the Interactive Brokers XML FlexQueries
    """
    def __init__(self,
                Mainaccount, 
                currency='CHF',
                divSuffix = 'Div', # suffix for dividend Account , like Assets:Invest:IB:VT:Div
                WHTSuffix = 'WTax', # 
                fpath=None,  # 
                deposits=False
                ):

        self.Mainaccount = Mainaccount # main IB account in beancount
        self.currency = currency # main currency of IB account
        self.divSuffix = divSuffix
        self.WHTSuffix = WHTSuffix
        self.filepath=fpath # optional file path specification, 
            # if flex query should not be used online (loading time...)
        self.deposits = deposits # Cash deposits are usually already covered
            # by checkings account statements. If you want anyway the 
            # deposit transactions, provide a True value 
        self.flag = '*' 

    def identify(self, file):
        return 'ibkr.yaml' == path.basename(file.name)

    def getLiquidityAccount(self,currency):
        return ':'.join([self.Mainaccount , currency  ])
    
    def getDivIncomeAcconut(self,currency,symbol):
        return ':'.join([self.Mainaccount.replace('Asset','Income') , symbol , currency])

    def getProductAccount(self,symbol):
        return ':'.join([self.Mainaccount , symbol , symbol])

    def getWHTAccount(self,symbol,currency):
        return ':'.join([self.Mainaccount.replace('Asset','Expenses') , symbol , self.WHTSuffix , currency])

    def file_account(self, _):
        return self.account
    
    def extract(self, credsfile, existing_entries=None):
        # the actual processing of the flex query
        
        # get the IBKR creentials ready
        try:
            with open(credsfile.name, 'r') as f:
                config = yaml.safe_load(f)
                token = config['token']
                queryId = config['queryId']
        except:
            warnings.warn('cannot read IBKR credentials file. Check filepath.')
            return[]

        # get prices of existing transactions, in case we sell something
        priceLookup = PriceLookup(existing_entries, config['baseCcy'])


        if self.filepath == None:
            # get the report from IB. might take a while, when IB is queuing due to 
            # traffic
            try:
                # try except in case of connection interrupt
                # Warning: queries sometimes take a few minutes until IB provides
                # the data due to busy servers
                response = client.download(token, queryId)
                statement = parser.parse(response)
            except:
                warnings.warn('could not fetch IBKR Statement. exiting.')
                # another option would be to try again
                return[]
            assert isinstance(statement, Types.FlexQueryResponse)
        else:
            print('loading from pickle')
            with open(self.filepath,'rb') as pf:
                statement = pickle.load(pf)
       
        # convert to dataframes
        poi=statement.FlexStatements[0] # point of interest
        reports=['CashReport','Trades','CashTransactions'] # relevant items from report
        tabs={report:pd.DataFrame([{key:val for key,val in entry.__dict__.items()} 
            for entry in poi.__dict__[report]]) 
                for report in reports}

        # pick the information (out of the big mess) that we really want
        ct_columns=['currency', 'symbol', 'description', 'isin', 'amount', 'type','reportDate']
        cr_columns=['currency', 'fromDate','toDate', 'endingCash']
        tr_columns=['symbol','description', 'isin', 'listingExchange', 'tradeDate', 'quantity',
        'tradePrice', 'proceeds', 'currency','ibCommission', 'ibCommissionCurrency',
        'netCash','transactionType','dateTime']

        # get single dataFrames
        ct=tabs['CashTransactions'][ct_columns]
        tr=tabs['Trades'][tr_columns]
        cr=tabs['CashReport'][cr_columns]

        div=ct[ct['type']==CashAction.DIVIDEND] # dividends only
        wht=ct[ct['type']==CashAction.WHTAX] # WHT only
        match=pd.merge(div, wht, on=['symbol','reportDate']) # matching WHT & div
        dep=ct[ct['type']==CashAction.DEPOSITWITHDRAW] # Deposits only
        int_=ct[ct['type']==CashAction.BROKERINTRCVD] # interest only

        transactions=[]
        # make dividend & WHT transactions
        for idx, row in match.iterrows():
            
            currency=row['currency_x']
            currency_wht=row['currency_y']
            if currency != currency_wht:
                warnings.warn('Warngin: Dividend currency {} ' +
                    'mismatches WHT currency {}. Skipping this' +  
                    'Transaction'.format(currency,currency_wht))
                continue
            symbol=row['symbol']
            
            amount_div=amount.Amount(row['amount_x'],currency)
            amount_wht=amount.Amount(row['amount_y'],currency)

            text=match['description_x'][2]
            isin=re.findall('([a-zA-Z]{2}[0-9]{10})',text)[0]
            pershare=re.search('(\d*[.]\d*)(\D*)(PER SHARE)', 
                                text, re.IGNORECASE).group(1)
            
            # make the postings, three for dividend/ wht transactions
            postings=[data.Posting(self.getDivIncomeAcconut(currency,
                                                            symbol),
                                    -amount_div, None, None, None, None),
                                   
                        data.Posting(self.getWHTAccount(symbol,
                                                        currency),
                                    -amount_wht, None, None, None, None),
                        data.Posting(self.getLiquidityAccount(currency),
                                    AmountAdd(amount_div,amount_wht),
                                    None, None, None, None)
                        ]
            meta=data.new_metadata('dividend',0,{'ISIN':isin,'per share':pershare})
            transactions.append(
                data.Transaction(meta, # could add div per share, ISIN,....
                            row['reportDate'],
                            self.flag,
                            symbol,     # payee
                            'Dividend '+symbol,
                            data.EMPTY_SET,
                            data.EMPTY_SET,
                            postings
                            ))
        
        return transactions