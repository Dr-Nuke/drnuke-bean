from beancount.ingest import extract
from drnukebean.importer import ibkr

# example importer config for IBKR importer by DrNuke
# use with "bean-extract ConfigIBKR_example.py /path/to/ibkr.yaml -f main.Ledgerfile

# your ibkr.yaml file schould look like 
# token: 123456789101112131415
# queryID: 123456
# baseCcy: XXX # 'USD', or 'CHF'

# Your IBKR flex Query needs to be XML and have the following fields selected:
# ct_columns=['type', 'currency', 'description', 'isin', 'amount', 'symbol','reportDate']
# cr_columns=['currency', 'fromDate','toDate', 'endingCash']
# tr_columns=['buySell', 'currency', 'symbol', 'description', 'tradeDate', 'quantity',
#        'tradePrice', 'ibCommission', 'ibCommissionCurrency', 'notes', 'cost',
#        'openDateTime', 'levelOfDetail', 'ibOrderID', 'proceeds', 
#        'dateTime']


# I do not know a nice way to specify the account structure. This importer is 
# based on my own. You can adjust the getXXXAccount() functions of the importer 
# class to suit your needs


IBKR = ibkr.IBKRImporter(
    Mainaccount = 'Assets:Invest:IB', # main IB account
    divSuffix = 'Div',          # suffix for dividend account, like
                                # Assets:Invest:IB:VT:Div
    WHTSuffix = 'WTax',         # suffix for WHT account
    interestSuffix='Interest',  # suffix for interest income account
    PnLSuffix='PnL',              # suffix for PnL Account
    FeesSuffix='Fees',          # suffix for fees & commisions
    currency = 'CHF',           # main currency
    depositAccount = '',        # put in your checkings account if you want deposit transactions
    fpath = 'testIB/ibfq.pk'    # use a pickle dump instead of the API, as it has
                                # considerable loading times. Set to None for real
                                # API Flex Query fetching. used mainly for development.
)
    
CONFIG = [IBKR]
extract.HEADER = '' # remove unnesseccary terminal output

