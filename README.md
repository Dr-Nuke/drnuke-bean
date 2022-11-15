# drnuke-bean
a repo for some beancount tool 

## Interactive Broker importer 
based on pandas, inspired by tariochbctools
This importer uses the IBKR FlexQuery service to fetch account information in API-style.

default output looks like
```
2020-03-31 * "VTI" "Dividend VTI"  
  ISIN: "US9220428588"  
  per share: "0.27920000"  
  Incomes:Invest:IB:VTI:USD         -10.00 USD  
  Expensess:Invest:IB:VTI:WTax:USD    1.50 USD  
  Assets:Invest:IB:USD                8.50 USD  
```
Insallation: make ibkr.py accessible for you python distro for importing. See the ConfigIBKR_example.py for some more guiding

## Postfinance Importer (Swiss)
Two importers for Postfinance Giro account and credit card. Since Postfinance (as of 2020) does not offer API-like access, it requires manual download of bank statements in .csv format

## FinPension Importer
Imports CSVs from Finpension (https://app.finpension.ch/documents/transactions)
Here you find example configs for 3 funds to set up a working example.


Commodities.bean:
```
; Finpension
1970-01-01 commodity CSIFEM
	name: "CSIF (CH) Equity Emerging Markets Blue DB"
	asset-class: "stock"
	price: "CHF:yahoo/0P0000A2DE.SW"
	isin: "CH0017844686"
	
1970-01-01 commodity CSIFWEXCH
	name: "CSIF (CH) III Equity World ex CH Blue - Pension Fund Plus ZB"
	asset-class: "stock"
	price: "CHF:yahoo/0P0001EDRL.SW"
	isin: "CH0429081620"

1970-01-01 commodity CSIFWEXCHSC
	name: "CSIF (CH) III Equity World ex CH Small Cap Blue - Pension Fund DB"
	asset-class: "stock"
	price: "CHF:yahoo/0P0000YXR4.SW"
	isin: "CH0214967314" 
```

Config.py:
```
from beancount.ingest import extract
from drnukebean.importer import finpension


FINPENSION = finpension.FinPensionImporter(
    Mainaccount='Assets:Invest:S2:Finpension',  # main IB account
    divSuffix='Div',            # suffix for dividend account, like Assets:Invest:IB:VT:Div
    interestSuffix='Interest',  # suffix for interest income account
    PnLSuffix='PnL',            # suffix for PnL Account
    FeesSuffix='Fees',          # suffix for fees & commisions
    currency='CHF',             # main currency
    depositAccount=None,
    ISIN_lookup={"CH0017844686": "CSIFEM",     # required to link ISIN with bean ticker
                 "CH0429081620": "CSIFWEXCH",
                 "CH0214967314": "CSIFWEXCHSC",
                 },
    file_encoding="utf-8-sig",
    sep=";",  # csv file separator
    # a regex pattern that allows to distinguish between pillar 2&3 and individual portfolios
    regex=r"finpension_(S[2,3][a-zA-Z0-9]?)_([A-Z][a-zA-Z]+\d)",
)

CONFIG = [FINPENSION]
extract.HEADER = ''  # remove unnesseccary terminal output
```

Main.bean:
make sure to use "NONE" booking as Finpension does not track lots (see http://furius.ca/beancount/doc/booking)
```
1970-07-07 open Assets:Invest:S2:Finpension:Portfolio1:CSIFWEXCH "NONE"
1970-07-07 open Assets:Invest:S2:Finpension:Portfolio1:CSIFEM "NONE"
1970-07-07 open Assets:Invest:S2:Finpension:Portfolio1:CSIFWEXCHSC "NONE"
```

## spread plugin
A plugin to distribute singele tansactions over a period of time.
I.e. distrbute an end-of-year Investment-account statement over the months of that year.
syntax is based on pandas.date_range, so you can use basic time series as provided with https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.date_range.html

use it within your ledger with
```
plugin "drnukebean.plugins.spreading" "{'liability_acc_base': 'Assets:Receivables:'}"
```
where the the parameter specifies the stem of the account that wil hold the temporary balance.

For example, distribute a quartely PnL statement over the last 3 months: 
```
2020-12-31 * "MyInvestmentAccount" "PnL"
  p_spreading_frequency: "M"         ; pd.date_range() flag for 'monthly'
  p_spreading_start: "2020-10-01"    ; makes October the start month
  p_spreading_times: "3"             ; tells the plugin to slpit into 3 transactions
  Assets:MyInvestmentAccount:CHF   1000 CHF
  Income:MyInvestmentAccount:PnL  -1000 CHF
```
becomes 
```

2020-12-31 * "MyInvestmentAccount" "PnL"
  p_spreading_frequency: "M"
  p_spreading_start: "2020-10-01"
  p_spreading_times: "3"
  Assets:Receivables:MyInvestmentAccount:PnL  -1000 CHF
  Assets:MyInvestmentAccount:CHF               1000 CHF

2020-10-31 * "MyInvestmentAccount" "PnL"
  p_spreading: "split 1000 into 3 chunks, M"
  Income:MyInvestmentAccount:PnL              -333.33 CHF
  Assets:Receivables:MyInvestmentAccount:PnL   333.33 CHF

2020-11-30 * "MyInvestmentAccount" "PnL"
  p_spreading: "split 1000 into 3 chunks, M"
  Income:MyInvestmentAccount:PnL              -333.33 CHF
  Assets:Receivables:MyInvestmentAccount:PnL   333.33 CHF

2020-12-31 * "MyInvestmentAccount" "PnL"
  p_spreading: "split 1000 into 3 chunks, M"
  Income:MyInvestmentAccount:PnL              -333.34 CHF
  Assets:Receivables:MyInvestmentAccount:PnL   333.34 CHF
```