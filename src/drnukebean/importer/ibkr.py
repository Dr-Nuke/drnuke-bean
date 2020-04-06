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
import numpy as np

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
from beancount.core import position
from beancount.core.number import MISSING

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

def minus(A):
    #a minus operator
    return amount.Amount(-A.number,A.currency)
class IBKRImporter(importer.ImporterProtocol):
    """
    Beancount Importer for the Interactive Brokers XML FlexQueries
    """
    def __init__(self,
                Mainaccount,  # for example Assets:Invest:IB
                currency='CHF',
                divSuffix = 'Div', # suffix for dividend Account , like Assets:Invest:IB:VT:Div
                WHTSuffix = 'WTax', # 
                interestSuffix='Interest',
                FeesSuffix='Fees',
                fpath=None,  # 
                depositAccount=''
                ):

        self.Mainaccount = Mainaccount # main IB account in beancount
        self.currency = currency # main currency of IB account
        self.divSuffix = divSuffix
        self.WHTSuffix = WHTSuffix
        self.interestSuffix=interestSuffix
        self.FeesSuffix=FeesSuffix
        self.filepath=fpath # optional file path specification, 
            # if flex query should not be used online (loading time...)
        self.depositAccount = depositAccount # Cash deposits are usually already covered
            # by checkings account statements. If you want anyway the 
            # deposit transactions, provide a True value 
        self.flag = '*' 

    def identify(self, file):
        return 'ibkr.yaml' == path.basename(file.name)

    def getLiquidityAccount(self,currency):
        # Assets:Invest:IB:USD
        return ':'.join([self.Mainaccount , currency])
    
    def getDivIncomeAcconut(self,currency,symbol):
        # Income:Invest:IB:VTI:Div
        return ':'.join([self.Mainaccount.replace('Assets','Income') , symbol , currency])

    def getInterestIncomeAcconut(self,currency):
        # Income:Invest:IB:USD
        return ':'.join([self.Mainaccount.replace('Assets','Income') ,self.interestSuffix, currency])

    def getAssetAccount(self,symbol):
        # Assets:Invest:IB:VTI
        return ':'.join([self.Mainaccount , symbol , symbol])

    def getWHTAccount(self,symbol):
        # Expenses:Invest:IB:VTI:WTax
        return ':'.join([self.Mainaccount.replace('Assets','Expenses') , symbol, self.WHTSuffix])

    def getFeesAccount(self,currency):
        # Expenses:Invest:IB:Fees:USD
        return ':'.join([self.Mainaccount.replace('Assets','Expenses') , self.FeesSuffix,currency])

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
            # continue # debugging
            
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

            text=row['description_x']
            isin=re.findall('([a-zA-Z]{2}[0-9]{10})',text)[0]
            pershare=re.search('(\d*[.]\d*)(\D*)(PER SHARE)', 
                                text, re.IGNORECASE).group(1)
            
            # make the postings, three for dividend/ wht transactions
            postings=[data.Posting(self.getDivIncomeAcconut(currency,
                                                            symbol),
                                    -amount_div, None, None, None, None),
                                   
                        data.Posting(self.getWHTAccount(symbol),
                                    -amount_wht, None, None, None, None),
                        data.Posting(self.getLiquidityAccount(currency),
                                    AmountAdd(amount_div,amount_wht),
                                    None, None, None, None)
                        ]
            meta=data.new_metadata('dividend',0,{'isin':isin,'per_share':pershare})
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

        # interest payments
        for idx, row in int_.iterrows():
            currency=row['currency']
            amount_=amount.Amount(row['amount'],currency)
            text=row['description']
            month=re.findall('\w{3}-\d{4}',text)[0]
            
            # make the postings, two for interest payments
            postings=[data.Posting(self.getInterestIncomeAcconut(currency),
                                    -amount_, None, None, None, None),
                        data.Posting(self.getLiquidityAccount(currency),
                                    amount_,None, None, None, None)
                        ]
            meta=data.new_metadata('Interest',0)
            transactions.append(
                data.Transaction(meta, # could add div per share, ISIN,....
                            row['reportDate'],
                            self.flag,
                            'IB',     # payee
                            ' '.join(['Interest ', currency , month]),
                            data.EMPTY_SET,
                            data.EMPTY_SET,
                            postings
                            ))

        # cash deposits
        # assumes you figured out how to deposit/ withdrawal without fees
        if len(self.depositAccount)>0: 
            for idx, row in dep.iterrows():
                currency=row['currency']
                amount_=amount.Amount(row['amount'],currency)
                
                # make the postings. two for deposits
                postings=[data.Posting(self.depositAccount,
                                        -amount_, None, None, None, None),
                            data.Posting(self.getLiquidityAccount(currency),
                                        amount_,None, None, None, None)
                            ]
                meta=data.new_metadata('deposit/withdrawel',0)
                transactions.append(
                    data.Transaction(meta, # could add div per share, ISIN,....
                                row['reportDate'],
                                self.flag,
                                'self',     # payee
                                "deposit / withdrawal",
                                data.EMPTY_SET,
                                data.EMPTY_SET,
                                postings
                                ))
        # Trades
        for idx, row in tr.iterrows():
            # continue # debugging
            currency = row['currency']
            currency_IBcommision = row['ibCommissionCurrency']
            symbol = row['symbol']
            proceeds = amount.Amount(row['proceeds'].__round__(2),currency)
            netcash = amount.Amount(row['netCash'].__round__(2),currency)
            commission=amount.Amount((row['ibCommission'].__round__(2)),currency_IBcommision)
            quantity = amount.Amount(row['quantity'],symbol)
            price = amount.Amount(row['tradePrice'],currency)
            text=row['description']
            
            if quantity.number >=0: # find out what we did
                is_sell = False
                buysell = 'buy'
                                    
                cost = position.CostSpec(
                    number_per=D(row['tradePrice']),
                    number_total=None,
                    currency=currency,
                    date=None,
                    label=None,
                    merge=False)
                posting_price=None
            else:
                is_sell =True
                buysell = 'sell'
                 # For sell transactions, rely on beancount to determine the matching lot.
                cost = position.CostSpec(
                    number_per=D(row['tradePrice']),
                    number_total=None,
                    currency=currency,
                    date=None,
                    label=None,
                    merge=False)
                posting_price=None # price
            
            # cost=position.CostSpec(None,None,None,price.__str__())
            # breakpoint()
            # make the postings. for one trade, four postings
            postings=[
                    data.Posting(self.getAssetAccount(symbol),
                        quantity, cost, posting_price, None, None),
                    data.Posting(self.getLiquidityAccount(currency),
                        proceeds, None, None, None, None),
                    data.Posting(self.getLiquidityAccount(currency_IBcommision),
                        commission, None, None, None, None),
                    data.Posting(self.getFeesAccount(currency),
                                        minus(commission),None, None, None, None)
                    ]
            meta=data.new_metadata('trade',0)
  
            transactions.append(
                data.Transaction(meta, # could add div per share, ISIN,....
                            row['dateTime'].date(),
                            self.flag,
                            symbol,     # payee
                            ' '.join([buysell, quantity.to_string() , '@', price.to_string() ]),
                            data.EMPTY_SET,
                            data.EMPTY_SET,
                            postings
                            ))
                
        # balances 
        for idx, row in cr.iterrows():
            currency=row['currency']
            if currency == 'BASE_SUMMARY':
                continue # this is a summary balance that is not needed for beancount
            amount_=amount.Amount(row['endingCash'].__round__(2),currency)
            
            # make the postings. two for deposits
            postings=[data.Posting(self.depositAccount,
                                    -amount_, None, None, None, None),
                        data.Posting(self.getLiquidityAccount(currency),
                                    amount_,None, None, None, None)
                        ]
            meta=data.new_metadata('balance',0)
            
            transactions.append(data.Balance(
                            meta,
                            row['toDate'] + timedelta(days=1), # see tariochtools EC imp.
                            self.getLiquidityAccount(currency),
                            amount_,
                            None,
                            None))
        return transactions