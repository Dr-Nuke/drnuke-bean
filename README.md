# drnuke-bean
a repo for some beancount tool 

## IBKR importer based on pandas, inspired by tariochbctools

dividends implemented. default output looks like
```
2020-03-31 * "VTI" "Dividend VTI"  
  ISIN: "US9220428588"  
  per share: "0.27920000"  
  Incomes:Invest:IB:VTI:USD         -10.00 USD  
  Expensess:Invest:IB:VTI:WTax:USD    1.50 USD  
  Assets:Invest:IB:USD                8.50 USD  
```
Insallation: make ibkr.py accessible for you python distro for importing. see the ConfigIBKR_example.py for some more guiding.

## [planned] postfinance importers, based on beancount-dkb



## [idea] Tutorial: getting started with beancount
A user-friendly start to a versatile and hackable accounting system.

I found switching to beancount (from GnuCash) to be an extremely tough project. The reasons were 
* At the same I swiched from Windows to Ubuntu, and had lots of trouble with it
* The documentation is decent only if you are not a Unix-greenhorn
* The source code is not easy to understand without some experience
* A lack of simple minimal working examples  
* There are not many people using it, and those who are are mostly advanced IT people with their own language

On the other hand, getting things to run is really rewarding and satisfying. The level of detail to which you can track your finances is amazing. Because for me this is still fresh, I'd like to share some of it here and hope to open up beancount to those with less experience in coding etc..