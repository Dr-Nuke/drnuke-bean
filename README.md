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

Caveats:
* the ending balance of a report can be ambigoues. I decided to just turn all balances of the last date into balance statements. the wrong ones need to be removed manually. This should not be a big effort, as the wrong ones are detected as such by bean-check/ fava
* the reports only deliver ISIN identifiers, no trackers etc.. The yahoo trackers of most funds are not allowed as currency in beancount syntax. Hence the user needs to make its own valid trackers, and supply a lookup against the according isin to the importer
* Finpension does not support lot tracking, hence no lot bookinig possible
* the csv report contains no information on the account or the pillar (ger: "Säule") 2 or 3a. hence this information must be passed to the interpreter in another way, I decided for a file name convention & regex detection. ugly, but it works

File name convention:

Renaming the transaction export such that the file names contain `finpension_SX_PortfolioY`, for example
```
finpension_S3a_Portfolio1_transaction_report__csv_file__(1).csv
```

Commodities.bean example:
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

Config.py example:
```
from beancount.ingest import extract
from drnukebean.importer import finpension


FINPENSION = finpension.FinPensionImporter(
    root_account='Assets:Invest:S2:Finpension:Portfolio1',  # "root" Finpens account
    deposit_account='Assets:YourBank:Checking', # where from deposits are coming
    div_suffix='Div',            # suffix for dividend account
    interest_suffix='Interest',  # suffix for interest income account
    pnl_suffix='PnL',            # suffix for PnL Account
    fees_suffix='Fees',          # suffix for fees & commisions
    isin_lookup={"CH0017844686": "CSIFEM",     # required to link ISIN with bean ticker
                 "CH0429081620": "CSIFWEXCH",
                 "CH0214967314": "CSIFWEXCHSC"},
    file_encoding="utf-8-sig",
    sep=";",  # csv file separator
    # a regex pattern that allows to distinguish between pillar 2&3 and individual portfolios
    # requires the first group to identify the pillar accounts, e.g. S2 or S3a
    # and the second group to identify the portfolio subaccount, e.g. Portfolio1
    regex=r"finpension_(S[2,3]a?)_([A-Z][a-zA-Z]+\d)"
)

CONFIG = [FINPENSION]
extract.HEADER = ''  # remove unnesseccary terminal output
```

Main.bean:
make sure to use "NONE" booking as Finpension does not track lots (see http://furius.ca/beancount/doc/booking) in order to prevent "No matching lot" errors
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