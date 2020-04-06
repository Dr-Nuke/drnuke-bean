from beancount.ingest import extract
from drnukebean.importer import ibkr

# example importer config for IBKR importer by DrNuke
# use with "bean-extract ConfigIBKR_example.py /path/to/ibkr.yaml -f main.Ledgerfile

# your ibkr.yaml file schould look like 
# token: 123456789101112131415
# queryID: 123456
# baseCcy: XXX # 'USD', or 'CHF'

# Your IBKR flex Query needs to be XML and have the following fields selected:
# Cash Transactios : ['currency', 'symbol', 'description', 'isin', 'amount', 'type','reportDate']
# Cash Reports : ['currency', 'fromDate','toDate', 'endingCash']
# Trades : ['symbol','description', 'isin', 'listingExchange', 'tradeDate', 'quantity',
# 'tradePrice', 'proceeds', 'currency','ibCommission', 'ibCommissionCurrency',
# 'netCash','transactionType','dateTime']


IBKR = ibkr.IBKRImporter(
    Mainaccount = 'Assets:Invest:IB', # main IB account
    divSuffix = 'Div', # suffix for dividend Account , like Assets:Invest:IB:VT:Div
    WHTSuffix = 'WTax', # suffix for WHT
    interestSuffix='Interest', # suffix for interest income
    FeesSuffix='Fees', # suffix for fees & commisions
    currency = 'CHF', # main currency
    deposits = False, # set True if you want transactions for cash deposits
    fpath = 'testIB/ibfq.pk' # use a pickle dump instead of the API, as it has
                             # considerable loading times.
)
    
CONFIG = [IBKR]
extract.HEADER = '' # remove unnesseccary terminal output

