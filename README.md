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

