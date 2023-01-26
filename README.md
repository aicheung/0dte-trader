# 0dte-trader
Trade 0DTE options algorithmically using Interactive Brokers (IBKR) API.

Supports the follwing option spreads:

- Bull Put / Bear Put
- Bear Call / Bear Call
- Butterfly
- Iron Condor / Iron Butterfly
- Long/Short single Put/Call option

Besides 0DTE options, this program can also trade longer dated version of the above spreads (just set `-e` to your desired DTE), as well as the following multi-DTE option spreads or stock-option combinations:

- Put Calendar / Diagonal Spreads
- Call Calendar / Diagonal Spreads
- Covered Call

## Installation
Python 3.10 is assumed. 
1. Create a virtual environment and then `pip install ibapi-10.20.1-py3-none-any.whl` to install the IBKR API.
2. Run the code by executing `python -m trading.Program` with the command line options or environment variables as specified below. e.g. to trade a Bull Put, run `python -m trading.Program -p 4002 -q 1 -t SPX -m 1 -s 0.05 -l 0.03 -x 3.0 -e 1`

## Command Line Arguments / Environment Variables

IBKR Gateway or TWS is assumed to be running in local machine or the same Kubernetes Pod (K8s setup config and instructions to automatically trade in the cloud will be available for a reasonable fee, TBD).

- `-p` (`PORT`): The TCP port to the IBKR Gateway or TWS.
- `-q` (`QUANTITY`): The amount of stock or futures option combos to trade
- `-d` (`DRY_RUN`): Dry run. If set (by passing "True"), all logic will execute as normal but will not send any orders to IBKR.
- `-m` (`MODE`): Mode: 1 for Bull / Bear Put, 2 for Bear / Bull Call, 3 for Iron Condor / Iron Butterfly, 4 for Butterfly, 5 - 8 for Short Put/Short Call/Long Put/Long Call respectively. 9 for Put Calendar/Diagonal Spread, 10 for Call Calendar/Diagonal Spread. 11 for Covered Call.
- `-s` (`SHORT_LEG_DELTA`): Delta of the short leg. Should be a float in range of [0,1]. For Iron Butterfly, set this to 0.5.
- `-l` (`LONG_LEG_DELTA`): Delta of the long leg. Should be a float in range of [0,1]. For Butterfly, should be the delta of the ITM option (i.e. larger than 0.5). Delta of the OTM leg will be determined by (1 - value).
- `-x` (`STOP_LOSS_PERCENTAGE`): Percentage of stop loss as the premium received. e.g. 3.0 for setting stop loss at 300 percent of premium received.
- `-e` (`DAY_TO_EXPIRY`): Day to expiry for the target option contract(s).
- `-fe` (`FAR_LEG_DAY_TO_EXPIRY`): For Calendar / Diagonal Spreads, the DTE of the far-dated leg.
- `-t` (`TICKER`): Ticker to trade. Can be US stocks or indexes with options only.
- `-ai` (`AUTO_RETRY_INTERVAL`): Due to the large bid-ask spread, option spread orders may have difficulty filling at mid prices. If set and is larger than zero, order will be resubmitted by the interval specified. In each interal, order price will be decremented by the amount specified by param `-ap`. If not set or set at 0, orders will wait indefinitely until completely filled.
- `-ap` (`AUTO_RETRY_PRICE_DECREMENT`): Decrements price towards 0 by the value specified each time the order is resubmitted. In other words, setting this to a negative value will increase the order price away from zero (useful for debit spreads).
- `-pt` (`PROFIT_TAKING_PERCENTAGE`): Percentage of premium for the profit taking order. e.g. 0.5 to take profit at 50% of credit received. If unset or set to 0, will not send profit taking orders to IBKR.
- `-ds` (`EXPIRY_DATE_SEARCH_RANGE`): If set and larger than 0, search for options with expiry dates plus or minus of this value if the DTE specified in the -e parameter is not found e.g. -e 30 and -ds 5, will search for options with DTE between 25-35.
