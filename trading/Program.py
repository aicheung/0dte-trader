"""
Copyright (C) 2019 Interactive Brokers LLC. All rights reserved. This code is subject to the terms
 and conditions of the IB API Non-Commercial License or the IB API Commercial License, as applicable.
"""

import argparse
import datetime
import collections
import inspect
import sys
import threading
from platform import uname
import copy
from operator import attrgetter
from decimal import Decimal
import os

import logging
import time
import os.path

from ibapi import wrapper
from ibapi.client import EClient
from ibapi.utils import longMaxString
from ibapi.utils import iswrapper

# types
from ibapi.common import * # @UnusedWildImport
from ibapi.order_condition import * # @UnusedWildImport
from ibapi.contract import * # @UnusedWildImport
from ibapi.order import * # @UnusedWildImport
from ibapi.order_state import * # @UnusedWildImport
from ibapi.execution import Execution
from ibapi.execution import ExecutionFilter
from ibapi.commission_report import CommissionReport
from ibapi.ticktype import * # @UnusedWildImport
from ibapi.tag_value import TagValue

from ibapi.account_summary_tags import *
from trading.IBKREvent import IBKREvent

from ibapi.scanner import ScanData

FUTURES_ROLLOVER_THREASHOLD = 14

def printWhenExecuting(fn):
    def fn2(self):
        print("   doing", fn.__name__)
        fn(self)
        print("   done w/", fn.__name__)

    return fn2

def printinstance(inst:Object):
    attrs = vars(inst)
    #print(', '.join('{}:{}'.format(key, decimalMaxString(value) if type(value) is Decimal else value) for key, value in attrs.items()))
    print(', '.join('{}:{}'.format(key, decimalMaxString(value) if type(value) is Decimal else
                                   floatMaxString(value) if type(value) is float else
                                   intMaxString(value) if type(value) is int else  
                                   value) for key, value in attrs.items()))

class Activity(Object):
    def __init__(self, reqMsgId, ansMsgId, ansEndMsgId, reqId):
        self.reqMsdId = reqMsgId
        self.ansMsgId = ansMsgId
        self.ansEndMsgId = ansEndMsgId
        self.reqId = reqId


class RequestMgr(Object):
    def __init__(self):
        # I will keep this simple even if slower for now: only one list of
        # requests finding will be done by linear search
        self.requests = []

    def addReq(self, req):
        self.requests.append(req)

    def receivedMsg(self, msg):
        pass


# ! [socket_declare]
class TestClient(EClient):
    def __init__(self, wrapper):
        EClient.__init__(self, wrapper)
        # ! [socket_declare]

        # how many times a method is called to see test coverage
        self.clntMeth2callCount = collections.defaultdict(int)
        self.clntMeth2reqIdIdx = collections.defaultdict(lambda: -1)
        self.reqId2nReq = collections.defaultdict(int)
        self.setupDetectReqId()

    def countReqId(self, methName, fn):
        def countReqId_(*args, **kwargs):
            self.clntMeth2callCount[methName] += 1
            idx = self.clntMeth2reqIdIdx[methName]
            if idx >= 0:
                sign = -1 if 'cancel' in methName else 1
                self.reqId2nReq[sign * args[idx]] += 1
            return fn(*args, **kwargs)

        return countReqId_

    def setupDetectReqId(self):

        methods = inspect.getmembers(EClient, inspect.isfunction)
        for (methName, meth) in methods:
            if methName != "send_msg":
                # don't screw up the nice automated logging in the send_msg()
                self.clntMeth2callCount[methName] = 0
                # logging.debug("meth %s", name)
                sig = inspect.signature(meth)
                for (idx, pnameNparam) in enumerate(sig.parameters.items()):
                    (paramName, param) = pnameNparam # @UnusedVariable
                    if paramName == "reqId":
                        self.clntMeth2reqIdIdx[methName] = idx

                setattr(TestClient, methName, self.countReqId(methName, meth))

                # print("TestClient.clntMeth2reqIdIdx", self.clntMeth2reqIdIdx)


# ! [ewrapperimpl]
class TestWrapper(wrapper.EWrapper):
    # ! [ewrapperimpl]
    def __init__(self):
        wrapper.EWrapper.__init__(self)

        self.wrapMeth2callCount = collections.defaultdict(int)
        self.wrapMeth2reqIdIdx = collections.defaultdict(lambda: -1)
        self.reqId2nAns = collections.defaultdict(int)
        self.setupDetectWrapperReqId()

    # TODO: see how to factor this out !!

    def countWrapReqId(self, methName, fn):
        def countWrapReqId_(*args, **kwargs):
            self.wrapMeth2callCount[methName] += 1
            idx = self.wrapMeth2reqIdIdx[methName]
            if idx >= 0:
                self.reqId2nAns[args[idx]] += 1
            return fn(*args, **kwargs)

        return countWrapReqId_

    def setupDetectWrapperReqId(self):

        methods = inspect.getmembers(wrapper.EWrapper, inspect.isfunction)
        for (methName, meth) in methods:
            self.wrapMeth2callCount[methName] = 0
            # logging.debug("meth %s", name)
            sig = inspect.signature(meth)
            for (idx, pnameNparam) in enumerate(sig.parameters.items()):
                (paramName, param) = pnameNparam # @UnusedVariable
                # we want to count the errors as 'error' not 'answer'
                if 'error' not in methName and paramName == "reqId":
                    self.wrapMeth2reqIdIdx[methName] = idx

            setattr(TestWrapper, methName, self.countWrapReqId(methName, meth))

            # print("TestClient.wrapMeth2reqIdIdx", self.wrapMeth2reqIdIdx)


# this is here for documentation generation
"""
#! [ereader]
        # You don't need to run this in your code!
        self.reader = reader.EReader(self.conn, self.msg_queue)
        self.reader.start()   # start thread
#! [ereader]
"""

# ! [socket_init]
class TradeApp(TestWrapper, TestClient):
    def __init__(self):
        TestWrapper.__init__(self)
        TestClient.__init__(self, wrapper=self)
        # ! [socket_init]
        self.nKeybInt = 0
        self.started = False
        self.nextValidOrderId = None
        self.permId2ord = {}
        self.reqId2nErr = collections.defaultdict(int)
        self.globalCancelOnly = False
        self.simplePlaceOid = None
        self.check_positions_only = False
        self.dry_run = False
        self.request_events = dict()
        self.contract = None #target contract to trade
        self.options_trading_mode = 1
        self.option_chain = {} #the current option chain
        self.short_leg_delta = 0.0
        self.long_leg_delta = 0.0
        self.stop_loss_percentage = 0.0
        self.dte = 0
        self.auto_retry_fill_interval = 0
        self.auto_retry_price_decrement = 0.05
        self.profit_taking_percentage = 0.0

        self.round_interval = 0.05

        self.tickers = []
        self.futures = []

        #current trading item:
        self.ticker = "" #stock ticker as passed from CMD or function call, to be searched for a valid contract
        self.future = ""
        self.quantity = 0

    def dumpTestCoverageSituation(self):
        for clntMeth in sorted(self.clntMeth2callCount.keys()):
            logging.debug("ClntMeth: %-30s %6d" % (clntMeth,
                                                   self.clntMeth2callCount[clntMeth]))

        for wrapMeth in sorted(self.wrapMeth2callCount.keys()):
            logging.debug("WrapMeth: %-30s %6d" % (wrapMeth,
                                                   self.wrapMeth2callCount[wrapMeth]))

    def dumpReqAnsErrSituation(self):
        logging.debug("%s\t%s\t%s\t%s" % ("ReqId", "#Req", "#Ans", "#Err"))
        for reqId in sorted(self.reqId2nReq.keys()):
            nReq = self.reqId2nReq.get(reqId, 0)
            nAns = self.reqId2nAns.get(reqId, 0)
            nErr = self.reqId2nErr.get(reqId, 0)
            logging.debug("%d\t%d\t%s\t%d" % (reqId, nReq, nAns, nErr))

    @iswrapper
    # ! [connectack]
    def connectAck(self):
        if self.asynchronous:
            self.startApi()

    # ! [connectack]

    @iswrapper
    # ! [nextvalidid]
    def nextValidId(self, orderId: int):
        super().nextValidId(orderId)

        logging.debug("setting nextValidOrderId: %d", orderId)
        self.nextValidOrderId = orderId
        print("NextValidId:", orderId)
    # ! [nextvalidid]

        # we can start now
        if hasattr(self, 'account'):
            self.start()

    def register_event(self, event: IBKREvent):
        """
        Register a new event, to be notified when IBKR operation completes
        """
        self.request_events[event.order_id] = event

    def notify_event(self, id):
        """
        Notify a event has been completed.
        """
        if id in self.request_events.keys():
            self.request_events[id].set()

    def find_event_by_id(self, id):
        if not id in self.request_events.keys():
            return None

        return self.request_events[id]

    def quit(self):
        """
        Disconnect from IBKR and quit app.
        """
        self.disconnect()
        sys.exit()

    def get_position_diff(self, original: int, target: int):
        """
        Get the difference between original (current) and target contract amounts for trading.

        Args:
            original (int): original amount
            target (int): target amount

        Returns:
            The difference between target - original
        """
        return target - original

    def round_price(self, price:float):
        """
        If price is not in round_interval , round price to nearest interval.
        e.g. -0.775 -> -0.8
        """
        pd = Decimal(str(round(price, 2)))
        ri = Decimal(str(self.round_interval))

        if pd % ri != Decimal('0'): 
            n_interval = round(abs(pd) / ri)
            result = float(n_interval * ri)
            return result * (-1 if price < 0 else 1)
        else:
            return float(pd)

    def execute_market_order(self, contract: Contract, quantity:int):
        """
        Execute a market order.

        Args:
            contract (Contract object): the target contract to buy or sell.
            quantity (int): target size to buy or sell (negative in sell case).
        """
        logging.info("Submitting new order...")
        trade_qty = abs(quantity)
        operation = "SELL" if quantity < 0  else "BUY"
        order_id = self.nextOrderId()
        new_order_event = threading.Event()
        new_order_event.contract = contract
        new_order_event.quantity = trade_qty
        new_order_event.order_id = order_id
        self.request_events[order_id] = new_order_event
        logging.info(f"NEW ORDER: {operation} {trade_qty} {contract.symbol} CONTRACTS")     
        if trade_qty == 0:
            logging.warning("0 Contracts to trade, no order will be submitted to IBKR!")
        elif self.dry_run:
            logging.warning("Dry run. No orders will be submitted to IBKR!")

        if not self.dry_run and not trade_qty == 0:
            self.placeOrder(order_id, contract,
                OrderSamples.MarketOrder(operation, trade_qty))
            new_order_event.wait()

    def execute_limit_order(self, contract: Contract, order: Order, stop_order: Order = None, profit_taker: Order = None):
        """
        Execute a limit order.

        Args:
            contract (Contract): the target contract to buy or sell
            order (Order): the target order
            stop_order (Order, optional): any attached stop order (e.g. STP) related to original order, will not wait for it to fill
            profit_taker (Order, optional): any attached profit taker order related to original order, will not wait for it to fill
        """
        logging.info("Submitting new limit order...")
        event = IBKREvent(self.nextOrderId())
        if order.totalQuantity == 0:
            logging.warning("0 Contracts to trade, no order will be submitted to IBKR!")
        elif self.dry_run:
            logging.warning("Dry run. No orders will be submitted to IBKR!")

        if not self.dry_run and order.totalQuantity != 0:
            order.orderId = event.order_id
            self.register_event(event)
            self.placeOrder(event.order_id, contract, order)
            
            if not stop_order == None:
                stop_order_id = self.nextOrderId() #needed for IB but not tracked
                stop_order.orderId = stop_order_id
                stop_order.parentId = order.orderId
                self.placeOrder(stop_order_id, contract, stop_order)
            
            if not profit_taker == None:
                profit_taker_order_id = self.nextOrderId()
                profit_taker.orderId = profit_taker_order_id
                profit_taker.parentId = order.orderId
                self.placeOrder(profit_taker_order_id, contract, profit_taker)

            done = False
            while not done:
                done = event.wait(self.auto_retry_fill_interval if self.auto_retry_fill_interval > 0 else None)
                if done:
                    break

                #retry
                order.lmtPrice = order.lmtPrice + (self.auto_retry_price_decrement * (1 if order.lmtPrice < 0 else -1))
                order.lmtPrice = self.round_price(order.lmtPrice)
                if not stop_order == None:
                    stop_order.auxPrice = self.round_price(order.lmtPrice * self.stop_loss_percentage)
                if not profit_taker == None:
                    profit_taker.lmtPrice = self.round_price(order.lmtPrice * self.profit_taking_percentage)
                    
                logging.warning(f"Order did not fill after specified interval. Retrying with price {order.lmtPrice}")
                event.clear()
                self.placeOrder(order.orderId, contract, order)
                if not stop_order == None:
                    self.placeOrder(stop_order.orderId, contract, stop_order)
                if not profit_taker == None:
                    self.placeOrder(profit_taker.orderId, contract, profit_taker)

    def make_daily_trade(self):
        """
        Close existing positions then open the new daily postision.
        Rollover is also handled if existing positions is too close to expiry
        (as defined by the FUTURES_ROLLOVER_THREASHOLD constant).
        """
        logging.info("Getting list of expiry dates...")
        expirys = self.get_future_expiry_dates()
        rollover_expirys = [] #expriys that need rollover
        for cnt in expirys:
            # Define the format of the string
            ex = cnt.lastTradeDateOrContractMonth
            format_string = '%Y%m%d'

            # Convert the string to a datetime object
            expdate = datetime.datetime.strptime(ex, format_string).date()
            diff = expdate - datetime.date.today()
            if diff.days < FUTURES_ROLLOVER_THREASHOLD:
                logging.warning(f"{self.get_daily_trade_contract().symbol} expiry {expdate} is too close. Will roll over existing contracts!")
                rollover_expirys.append(ex)
            else:
                self.contract = cnt
                logging.info(f"Using {self.get_daily_trade_contract().lastTradeDateOrContractMonth} for new trades.")
                break

        logging.info("Getting existing positions...")
        position_event = threading.Event()
        position_event.contract = None
        position_event.extra_contracts = []
        self.request_events["POS"] = position_event
        self.reqPositions()
        position_event.wait()
        logging.info("Get existing positions done.")

        if self.check_positions_only:
            self.disconnect()
            return

        #rollover handling
        for ct in position_event.extra_contracts:
            contract = ct[0]
            size = ct[1]
            if size == 0: continue #already closed, but IBKR will still return the position today
            if contract.lastTradeDateOrContractMonth in rollover_expirys:
                #close position
                #fillback exchange
                contract.exchange = self.get_daily_trade_contract().exchange
                self.execute_market_order(contract, -1 * size)

        #for adjustment we only consider same expiry dates as rollover is already handled.
        cur_contract = 0
        if (position_event.contract 
            and not position_event.quantity == 0 
            and position_event.contract.lastTradeDateOrContractMonth == self.get_daily_trade_contract().lastTradeDateOrContractMonth) :
            logging.info(f"Found existing {position_event.contract.symbol} contract: {position_event.quantity}")
            cur_contract = position_event.quantity

        logging.info("Submitting new order...")
        quantity = int(self.quantity)
        difference = self.get_position_diff(cur_contract, quantity)
        self.execute_market_order(self.get_daily_trade_contract(), difference)
        logging.info("Operation end")

    def search_contracts(self, search_criteria: Contract):
        """
        Search for contracts given the specified criteria.

        Args:
            search_criteria (Contract object): the contract object containing the search criteria.
        
        Returns:
            A list of ContractDetails objects if found (with Contract object inside). Else a empty list is returned.
        """
        
        event = threading.Event()
        event.order_id = self.nextOrderId()
        event.contract_details = []
        self.request_events[event.order_id] = event
        self.reqContractDetails(event.order_id, contract=search_criteria)
        event.wait()

        return event.contract_details

    def get_future_expiry_dates(self):
        """
        Get the list of expiry dates for the target future contract.

        Returns:
            A list of contracts ordered by expiry dates.
            If the contract is not futures i.e. stock, returns a empty list.

        """
        contract = self.get_daily_trade_contract()

        if not contract.secType == "FUT":
            logging.warning("Contract is not a futures contract. No expiry dates found!")
            return []

        contract = copy.copy(contract)
        #remove expiry, local symbol and id for search
        contract.lastTradeDateOrContractMonth = ""
        contract.localSymbol = ""
        contract.conId = ""

        contracts = [cd.contract for cd in self.search_contracts(contract)]

        contracts = sorted(contracts, key=attrgetter("lastTradeDateOrContractMonth"))
        return contracts

    def get_target_contract(self):
        """
        Search for a valid IBKR Contract if self.ticker is specified.
        If not specified or no contracts found, default is to trade ES futures.
        """
        if self.ticker == "" and self.future == "":
            return

        contract = Contract()
        contract.symbol = self.ticker if self.future == "" else self.future
        contract.secType = "IND" if self.future == "" else "FUT"
        #contract.currency = "USD"
        contracts = self.search_contracts(contract)

        if len(contracts) == 0:
            logging.warning(f"No {contract.secType} contract found for symbol {contract.symbol}!")

            contract.secType = "STK"
            contracts = self.search_contracts(contract)

            if len(contracts) == 0:
                logging.warning(f"No {contract.secType} contract found for symbol {contract.symbol}!")
                raise BaseException(f"No stock, future or index contract found for {contract.symbol}!")

        target_con = contracts[0].contract
        self.contract = target_con
        logging.info(f"Will use the following contract for trading: {target_con}")

    def get_price(self, contract: Contract):
        e = IBKREvent(self.nextOrderId(), contract)
        self.register_event(e)
        self.reqMktData(e.order_id, contract, "", False, False, None)
        e.wait()
        return e.last, e.option_bid, e.option_ask

    def get_option_data(self, exp, strikes, side, trading_class, itm_options: bool = False, get_prices: bool = False):
        #for optimization, no need to get far strike options? (e.g. 7% range for SPX)
        target_range_percentage = 0.07

        #get the underlying prices and get only relavent options
        cur_price, bid, ask = self.get_price(self.contract)
        target_max = cur_price * (1 + target_range_percentage)
        target_min = cur_price * (1 - target_range_percentage)

        opts = []
        submitted = 0
        skip_itm = self.short_leg_delta < 0.51 and self.long_leg_delta < 0.51
        for s in strikes:
            if s > target_max or s < target_min:
                continue

            if not itm_options and side == "P" and skip_itm and s > cur_price:
                continue #no itm put

            if not itm_options and side == "C" and skip_itm and s < cur_price:
                continue #no itm call

            contract = Contract()
            contract.symbol = self.contract.symbol
            contract.secType = "OPT"
            contract.exchange = "SMART"
            contract.currency = "USD"
            contract.lastTradeDateOrContractMonth = exp
            contract.strike = s
            contract.tradingClass = trading_class
            contract.right = side

            e = IBKREvent(self.nextOrderId())
            e.contract = contract
            e.get_prices = get_prices
            self.register_event(e)
            self.reqMktData(e.order_id, contract, "225", False, False, None)
            opts.append(e)
            submitted += 1
            if submitted % 20 == 0:
                time.sleep(1)

        for e in opts:
            e.wait()

        for e in opts:
            if e.option_delta != None:
                logging.info(f"Strike {e.contract.strike}{e.contract.right} done! {e.option_bid} {e.option_ask} {e.option_delta}")
                self.option_chain[str(e.contract.strike) + e.contract.right] = {
                    "bid": e.option_bid,
                    "ask": e.option_ask,
                    "delta": e.option_delta,
                    "contract": e.contract
                }

    def get_target_option_chain(self, dte: int = 0, get_prices: bool = False):
        """
        Get the option chain for the specified DTE for the target contract.
        """
        target_exp = datetime.date.today() + datetime.timedelta(days = dte)
        target_exp_str = target_exp.strftime("%Y%m%d")
        event = IBKREvent(self.nextOrderId())
        event.target_expiration = target_exp_str
        self.register_event(event)
        self.reqSecDefOptParams(event.order_id, self.contract.symbol, "", self.contract.secType, self.contract.conId)
        event.wait()
        logging.info(f"Option chain: {event.option_strikes}")
        logging.info(f"Option expirations: {event.option_expirations}")

        has_exp = target_exp_str in event.option_expirations
        if not has_exp:
            logging.warning(f"Expiration date {target_exp_str} not found for ticker: {self.contract.symbol}. Will not trade!")
            return False

        #load greeks for all calls and puts?
        logging.info(f"Loading Greeks for {self.contract.symbol} options with expiry {target_exp_str}:")

        if self.options_trading_mode in [1,3,5,7]:
            self.get_option_data(target_exp_str, event.option_strikes, "P", event.option_trading_class, get_prices)
        if self.options_trading_mode in [2,3,4,6,8]:
            self.get_option_data(target_exp_str, event.option_strikes, "C", event.option_trading_class, get_prices) #may not be needed for 0dte put spread?

        return True

    def find_option_by_delta(self, delta: Decimal | float, side: str, delta_upper_bound: Decimal = None, delta_lower_bound: Decimal = None):
        """
        Find the option contract with the closest delta as specified.

        Args:
            delta (Decimal or float): the delta of the contract
            side (str): "P" for put, "C" for call
            delta_upper_bound (Decimal, optional): the upper bound for delta. Can be used to limit a particular leg so as not to get the same contract as other legs.
            delta_lower_bound (Decimal, optional): the lower bound for delta. Can be used to limit a particular leg so as not to get the same contract as other legs.
        
        Returns:
            The dict containing the target option contract in the "contract" key.
        """
        assert(side in ["C", "P"])

        delta = round(Decimal(delta),2) if type(delta) == float else delta
        diff = Decimal(9999999.99)
        target = None
        for s in self.option_chain.keys():
            if str(s).find(side) == -1:
                #wrong side
                continue

            option = self.option_chain[s]
            delta_val = abs(option['delta'])
            if delta_upper_bound != None and delta_val >= abs(delta_upper_bound):
                continue
            elif delta_lower_bound != None and delta_val <= abs(delta_lower_bound):
                continue
            delta_diff = abs(abs(delta_val) - abs(delta))
            if delta_diff < diff:
                diff = delta_diff
                target = option

        return target

    def check_market_open(self):
        """
        Check if market is open today.

        Returns:
            True if market is open today. False otherwise.
        """
        now = datetime.date.today()
        #SPY for checking
        c = Contract()
        c.symbol = 'SPY'
        c.secType = 'STK'
        c.exchange = 'SMART'
        c.currency = 'USD'

        contracts = self.search_contracts(c)
        if len(contracts) == 0:
            #should not happen!!!
            logging.error(f"Cannot find contract: {c.symbol}")
            raise Exception("Cannot check market open times!")
        
        c: ContractDetails = contracts[0]

        # Create an empty dictionary to store the converted data
        hours_map = {}

        # Loop through each string in the hours list
        for hour_str in c.liquidHours.split(";"):
            # Split the string on the colon character to get the key and value
            k, v = hour_str.split(":", 1)

            # Add the key and value to the dictionary
            hours_map[k] = v

        today_str = now.strftime("%Y%m%d")
        hours_today = hours_map[today_str]

        return not hours_today == 'CLOSED'

    def create_order(self, contract, min_tick, stop_loss_percentage, is_long: bool = True):
        """
        Helper function for creating the IBKR order as well as related stop loss and/or profit taker.

        Args:
            contract (Contract): the IBKR contract object for the order.
            min_tick (float): the minimun tick as returned by the ContractDetails object.
            stop_loss_percentage (float): percentage of stop loss.
            is_long (bool, optional): is the order a long order.

        Returns:
            A tuple containing:
                1. The option contract
                2. IBKR order object for the order
                3. IBKR order object for the attached stop loss order
                4. IBKR order object for the profit taker order (if required)
        """
        
        #for rounding
        self.round_interval = min_tick

        last, bid, ask = self.get_price(contract)
        price = self.round_price((bid + ask) / 2.0)
        #assert(price < 0.0) # credit 
        stop_loss_price = self.round_price(price * stop_loss_percentage)

        order = Order()
        #order.orderId = self.nextOrderId()
        order.action = "BUY" if is_long else "SELL"
        order.orderType = "LMT"
        order.totalQuantity = self.quantity
        order.lmtPrice = price
        order.transmit = False #prevent race condition according to IBKR doc

        profit_taker = Order()
        profit_taker.action = "SELL" if is_long else "BUY"
        profit_taker.orderType = "LMT"
        profit_taker.totalQuantity = self.quantity
        profit_taker.lmtPrice = self.round_price(price * self.profit_taking_percentage)
        profit_taker.transmit = False
        
        stop_order = Order()
        #stop_order.orderId = self.nextOrderId()
        #stop_order.parentId = order.orderId
        stop_order.action = "SELL" if is_long else "BUY"
        stop_order.orderType = "STP"
        stop_order.totalQuantity = self.quantity
        stop_order.auxPrice = stop_loss_price
        stop_order.transmit = True

        return (contract, order, stop_order, None if self.profit_taking_percentage == 0.0 else profit_taker)

    def create_option_spread_order(self, underlying_symbol: str, currency: str, spread: list[ComboLeg], stop_loss_percentage: float, min_tick: float):
        """
        Create a IBKR order with stop loss as attched order from the spread legs.

        Args:
            underlying_symbol (str): the ticker for the underlying
            currency (str): currency of the option contract(s)
            spread (list[ComboLeg]): the list of option legs in the spread.
            stop_loss_percentage (float): percentage of stop loss as premium received e.g. 3.0 for 300% stop loss as premium received.
            min_tick (float): the minimum increment of price of the spread.

        Returns:
            A tuple containing:
                1. The option combo contract
                2. IBKR order object for the order
                3. IBKR order object for the attached stop loss order
                4. IBKR order object for the profit taker order (if required)

        """
        assert(spread != None and len(spread) > 1)
        contract = Contract()
        contract.symbol = underlying_symbol
        contract.secType = "BAG"
        contract.currency = currency
        contract.exchange = "SMART"

        contract.comboLegs = spread

        return self.create_order(contract, min_tick, stop_loss_percentage, True)

    def create_credit_spread_order(self, short_leg: dict | list[dict], long_leg: dict | list[dict], stop_loss_percentage: float, ratio: tuple = (1, 1)):
        """
        Create a IBKR spread (Bull Put, Bear Call or Iron Condor etc.) order with stop loss as attached order.

        Args:
            short_leg (dict | list[dict]): the dict or list of it with the short leg option contract(s) in the "contract" key.
            long_leg (dict | list[dict]): the dict or the list of it with the long leg option contract(s) in the "contract" key.
            stop_loss_percentage (float): percentage of stop loss as premium received e.g. 3.0 for 300% stop loss as premium received.
            ratio (tuple, optional): tuple containing the ratio between long to short legs e.g. (1, 1) for a Bull Put order

        Returns:
            A tuple containing:
                1. The spread option combo contract
                2. IBKR order object for the spread order
                3. IBKR order object for the attached stop loss order
                4. IBKR order object for the attached profit taker order
        """
        assert(short_leg != None)
        assert(long_leg != None)
        long_ratio = ratio[0]
        short_ratio = ratio[1]

        spread = []
        shorts: list = short_leg if type(short_leg) == list else [short_leg]
        longs: list = long_leg if type(long_leg) == list else [long_leg]
        
        #to be passed to order
        min_tick = None

        for s in shorts + longs:
            contract_details: ContractDetails = self.search_contracts(s['contract'])[0]

            if min_tick == None:
                min_tick = contract_details.minTick

            leg = ComboLeg()
            leg.conId = contract_details.contract.conId
            leg.ratio = long_ratio if s in longs else short_ratio
            leg.action = "SELL" if s in shorts else "BUY"
            leg.exchange = "SMART"
            spread.append(leg)

        return self.create_option_spread_order(
            self.contract.symbol, 
            self.contract.currency, 
            spread, 
            stop_loss_percentage, 
            min_tick
            )

    def execute_single_option_order(self, contract_dict: dict, stop_loss_percentage: float, is_short: bool = True):
        """
        Create a single option order with stop loss as attached order.

        Args:
            short_leg (dict | list[dict]): the dict or list of it with the short leg option contract(s) in the "contract" key.
            long_leg (dict | list[dict]): the dict or the list of it with the long leg option contract(s) in the "contract" key.
            stop_loss_percentage (float): percentage of stop loss as premium received e.g. 3.0 for 300% stop loss as premium received.
            ratio (tuple, optional): tuple containing the ratio between long to short legs e.g. (1, 1) for a Bull Put order

        Returns:
            A tuple containing:
                1. The single option contract
                2. IBKR order object for the single option order
                3. IBKR order object for the attached stop loss order
                4. IBKR order object for the attached profit taker order
        """
        contract_details = self.search_contracts(contract_dict['contract'])[0]

        #to be passed to order
        min_tick = None

        if min_tick == None:
            min_tick = contract_details.minTick

        contract = contract_details.contract

        combo_contract, order, stop_loss_order, profit_taker = self.create_order(contract, min_tick, stop_loss_percentage, not is_short)
        assert(order != None and stop_loss_order != None)
        self.execute_limit_order(combo_contract, order, stop_loss_order, profit_taker)

    def execute_option_spread_order(self, short_leg: dict | list, long_leg: dict | list, ratio: tuple = (1, 1)):
        """
        Execute a spread order given long and short option legs.

        Args:
            short_leg (dict | list[dict]): dict or list of it containing the 'contract' field for the Contract object for the short option(s).
            long_leg (dict | list[dict]): dict or list of it containing the 'contract' field for the Contract object for the long option(s).
            ratio (tuple, optional): tuple containing the ratio between long to short legs e.g. (1, 1) for a Bull Put order
        """
        short_leg = [short_leg] if type(short_leg) == dict else short_leg
        long_leg = [long_leg] if type(long_leg) == dict else long_leg
        assert(short_leg != None and long_leg != None)
        logging.info(f"Spread order:")
        logging.info(f"- Short strike(s): {','.join([str(s['contract'].strike) for s in short_leg])}")
        logging.info(f"- Long strike(s): {','.join([str(s['contract'].strike) for s in long_leg])}")
        combo_contract, order, stop_loss_order, profit_taker = self.create_credit_spread_order(short_leg, long_leg, self.stop_loss_percentage, ratio)
        assert(order != None and stop_loss_order != None)
        self.execute_limit_order(combo_contract, order, stop_loss_order, profit_taker)

    def trading_thread(self):
        """
        The thread to run trading logic, so as to not block the IBKR threads.
        """
        market_open = self.check_market_open()
        if not market_open:
            logging.warning("Market is not open! Will not trade!")
            self.disconnect()
            return

        for item in self.tickers:
            self.option_chain = {}
            self.ticker = item[0]
            self.future = ""
            self.quantity = item[1]
            self.get_target_contract()
            option_chain_found = self.get_target_option_chain(self.dte) #0 for 0dte

            if not option_chain_found:
                continue

            #now we have option chain, build option combo and then order by mode
            order = None
            match self.options_trading_mode:
                case 1:
                    #bull / bear put
                    #1x short put + 1x long put 
                    short_leg = self.find_option_by_delta(self.short_leg_delta, "P")
                    long_leg = self.find_option_by_delta(
                        self.long_leg_delta, 
                        "P", 
                        delta_upper_bound=None if self.short_leg_delta < self.long_leg_delta else short_leg['delta'],
                        delta_lower_bound=None if self.short_leg_delta > self.long_leg_delta else short_leg['delta'])
                    self.execute_option_spread_order(short_leg, long_leg)
                case 2:
                    #bear / bull call
                    #1x short call + 1x long call 
                    short_leg = self.find_option_by_delta(self.short_leg_delta, "C")
                    long_leg = self.find_option_by_delta(
                        self.long_leg_delta, 
                        "C", 
                        delta_upper_bound=None if self.short_leg_delta < self.long_leg_delta else short_leg['delta'],
                        delta_lower_bound=None if self.short_leg_delta > self.long_leg_delta else short_leg['delta'])
                    self.execute_option_spread_order(short_leg, long_leg)
                case 3:
                    #iron condor
                    #1x short put + 1x long put
                    #1x short call + 1x long call 
                    #call and put spreads filled separately
                    short_call_leg = self.find_option_by_delta(self.short_leg_delta, "C")
                    long_call_leg = self.find_option_by_delta(self.long_leg_delta, "C", delta_upper_bound=short_call_leg['delta'])
                    short_put_leg = self.find_option_by_delta(self.short_leg_delta, "P")
                    long_put_leg = self.find_option_by_delta(self.long_leg_delta, "P", delta_upper_bound=short_put_leg['delta'])
                    assert(short_call_leg != None and long_call_leg != None and short_put_leg != None and long_put_leg != None)
                    self.execute_option_spread_order(short_call_leg, long_call_leg)
                    self.execute_option_spread_order(short_put_leg, long_put_leg)
                case 4:
                    #Iron butterfly
                    otm_leg_delta = 1 - self.long_leg_delta
                    itm_leg = self.find_option_by_delta(self.long_leg_delta, "C")
                    atm_leg = self.find_option_by_delta(0.5, "C")
                    otm_leg = self.find_option_by_delta(otm_leg_delta, "C")
                    self.execute_option_spread_order(atm_leg, [itm_leg, otm_leg], (1, 2))
                case 5 | 6 | 7 | 8:
                    #single put/call order
                    short_put_leg = self.find_option_by_delta(self.short_leg_delta, "P" if self.options_trading_mode in [5,7] else "C")
                    assert(short_put_leg != None)
                    self.execute_single_option_order(short_put_leg, self.stop_loss_percentage, True if self.options_trading_mode in [5,6] else False)
        time.sleep(5) #for IBKR to clear pending messages
        logging.info("All trades complete! Disconnecting now...")
        self.disconnect()

    def start(self):
        if self.started:
            return

        self.started = True

        if self.globalCancelOnly:
            print("Executing GlobalCancel only")
            self.reqGlobalCancel()
        else:
            
            print("Executing requests")
            thread = threading.Thread(target=self.trading_thread, name="TradingThread")
            thread.start()
            
            print("Executing requests ... finished")

    def keyboardInterrupt(self):
        self.nKeybInt += 1
        if self.nKeybInt == 1:
            self.stop()
        else:
            print("Finishing test")
            self.done = True

    def stop(self):
        pass #placeholder for IBKR code

    def nextOrderId(self):
        oid = self.nextValidOrderId
        self.nextValidOrderId += 1
        return oid

    @iswrapper
    # ! [error]
    def error(self, reqId: TickerId, errorCode: int, errorString: str, advancedOrderRejectJson = ""):
        super().error(reqId, errorCode, errorString, advancedOrderRejectJson)
        if advancedOrderRejectJson:
            print("Error. Id:", reqId, "Code:", errorCode, "Msg:", errorString, "AdvancedOrderRejectJson:", advancedOrderRejectJson)
        else:
            print("Error. Id:", reqId, "Code:", errorCode, "Msg:", errorString)

        if reqId in self.request_events:
            event = self.request_events[reqId]
            if not event.is_set():
                #cancel it
                event.set()

    # ! [error] self.reqId2nErr[reqId] += 1


    @iswrapper
    def winError(self, text: str, lastError: int):
        super().winError(text, lastError)

    @iswrapper
    # ! [openorder]
    def openOrder(self, orderId: OrderId, contract: Contract, order: Order,
                  orderState: OrderState):
        super().openOrder(orderId, contract, order, orderState)
        print("OpenOrder. PermId:", intMaxString(order.permId), "ClientId:", intMaxString(order.clientId), " OrderId:", intMaxString(orderId), 
              "Account:", order.account, "Symbol:", contract.symbol, "SecType:", contract.secType,
              "Exchange:", contract.exchange, "Action:", order.action, "OrderType:", order.orderType,
              "TotalQty:", decimalMaxString(order.totalQuantity), "CashQty:", floatMaxString(order.cashQty), 
              "LmtPrice:", floatMaxString(order.lmtPrice), "AuxPrice:", floatMaxString(order.auxPrice), "Status:", orderState.status,
              "MinTradeQty:", intMaxString(order.minTradeQty), "MinCompeteSize:", intMaxString(order.minCompeteSize),
              "competeAgainstBestOffset:", "UpToMid" if order.competeAgainstBestOffset == COMPETE_AGAINST_BEST_OFFSET_UP_TO_MID else floatMaxString(order.competeAgainstBestOffset),
              "MidOffsetAtWhole:", floatMaxString(order.midOffsetAtWhole),"MidOffsetAtHalf:" ,floatMaxString(order.midOffsetAtHalf))

        order.contract = contract
        self.permId2ord[order.permId] = order
            
    # ! [openorder]

    @iswrapper
    # ! [openorderend]
    def openOrderEnd(self):
        super().openOrderEnd()
        print("OpenOrderEnd")

        logging.debug("Received %d openOrders", len(self.permId2ord))
    # ! [openorderend]

    @iswrapper
    # ! [orderstatus]
    def orderStatus(self, orderId: OrderId, status: str, filled: Decimal,
                    remaining: Decimal, avgFillPrice: float, permId: int,
                    parentId: int, lastFillPrice: float, clientId: int,
                    whyHeld: str, mktCapPrice: float):
        super().orderStatus(orderId, status, filled, remaining,
                            avgFillPrice, permId, parentId, lastFillPrice, clientId, whyHeld, mktCapPrice)
        print("OrderStatus. Id:", orderId, "Status:", status, "Filled:", decimalMaxString(filled),
              "Remaining:", decimalMaxString(remaining), "AvgFillPrice:", floatMaxString(avgFillPrice),
              "PermId:", intMaxString(permId), "ParentId:", intMaxString(parentId), "LastFillPrice:",
              floatMaxString(lastFillPrice), "ClientId:", intMaxString(clientId), "WhyHeld:",
              whyHeld, "MktCapPrice:", floatMaxString(mktCapPrice))

        if orderId in self.request_events:
            event = self.request_events[orderId]
            if event is not None and not event.is_set() and remaining == 0:
                event.set()
    # ! [orderstatus]

    @iswrapper
    # ! [managedaccounts]
    def managedAccounts(self, accountsList: str):
        super().managedAccounts(accountsList)
        print("Account list:", accountsList)
        # ! [managedaccounts]

        self.account = accountsList.split(",")[0]
        
        if self.nextValidOrderId is not None:
            self.start()

    @iswrapper
    # ! [accountsummary]
    def accountSummary(self, reqId: int, account: str, tag: str, value: str,
                       currency: str):
        super().accountSummary(reqId, account, tag, value, currency)
        print("AccountSummary. ReqId:", reqId, "Account:", account,
              "Tag: ", tag, "Value:", value, "Currency:", currency)
    # ! [accountsummary]

    @iswrapper
    # ! [accountsummaryend]
    def accountSummaryEnd(self, reqId: int):
        super().accountSummaryEnd(reqId)
        print("AccountSummaryEnd. ReqId:", reqId)
    # ! [accountsummaryend]

    @iswrapper
    # ! [updateaccountvalue]
    def updateAccountValue(self, key: str, val: str, currency: str,
                           accountName: str):
        super().updateAccountValue(key, val, currency, accountName)
        print("UpdateAccountValue. Key:", key, "Value:", val,
              "Currency:", currency, "AccountName:", accountName)
    # ! [updateaccountvalue]

    @iswrapper
    # ! [updateportfolio]
    def updatePortfolio(self, contract: Contract, position: Decimal,
                        marketPrice: float, marketValue: float,
                        averageCost: float, unrealizedPNL: float,
                        realizedPNL: float, accountName: str):
        super().updatePortfolio(contract, position, marketPrice, marketValue,
                                averageCost, unrealizedPNL, realizedPNL, accountName)
        print("UpdatePortfolio.", "Symbol:", contract.symbol, "SecType:", contract.secType, "Exchange:",
              contract.exchange, "Position:", decimalMaxString(position), "MarketPrice:", floatMaxString(marketPrice),
              "MarketValue:", floatMaxString(marketValue), "AverageCost:", floatMaxString(averageCost),
              "UnrealizedPNL:", floatMaxString(unrealizedPNL), "RealizedPNL:", floatMaxString(realizedPNL),
              "AccountName:", accountName)
    # ! [updateportfolio]

    @iswrapper
    # ! [updateaccounttime]
    def updateAccountTime(self, timeStamp: str):
        super().updateAccountTime(timeStamp)
        print("UpdateAccountTime. Time:", timeStamp)
    # ! [updateaccounttime]

    @iswrapper
    # ! [accountdownloadend]
    def accountDownloadEnd(self, accountName: str):
        super().accountDownloadEnd(accountName)
        print("AccountDownloadEnd. Account:", accountName)
    # ! [accountdownloadend]

    @iswrapper
    # ! [position]
    def position(self, account: str, contract: Contract, position: Decimal,
                 avgCost: float):
        super().position(account, contract, position, avgCost)
        print("Position.", "Account:", account, "Symbol:", contract.symbol, "SecType:",
              contract.secType, "Currency:", contract.currency,
              "Position:", decimalMaxString(position), "Avg cost:", floatMaxString(avgCost))

        if position == 0:
            return #don't care about already closed postions

        target_con = self.get_daily_trade_contract()

        if self.request_events["POS"] is not None and not self.request_events["POS"].is_set() and contract.symbol == target_con.symbol:
            target_exp = self.get_daily_trade_contract().lastTradeDateOrContractMonth

            if not contract.lastTradeDateOrContractMonth == target_exp:
                #contracts need rollover or far contracts
                self.request_events["POS"].extra_contracts.append((contract, position))
            else:
                print(f"position(): Found existing {contract.symbol} position!")
                self.request_events["POS"].contract = contract
                self.request_events["POS"].quantity = position
    # ! [position]

    @iswrapper
    # ! [positionend]
    def positionEnd(self):
        super().positionEnd()
        print("PositionEnd")
        if self.request_events["POS"] and not self.request_events["POS"].is_set():
            self.request_events["POS"].set()
    # ! [positionend]

    @iswrapper
    # ! [positionmulti]
    def positionMulti(self, reqId: int, account: str, modelCode: str,
                      contract: Contract, pos: Decimal, avgCost: float):
        super().positionMulti(reqId, account, modelCode, contract, pos, avgCost)
        print("PositionMulti. RequestId:", reqId, "Account:", account,
              "ModelCode:", modelCode, "Symbol:", contract.symbol, "SecType:",
              contract.secType, "Currency:", contract.currency, ",Position:",
              decimalMaxString(pos), "AvgCost:", floatMaxString(avgCost))
    # ! [positionmulti]

    @iswrapper
    # ! [positionmultiend]
    def positionMultiEnd(self, reqId: int):
        super().positionMultiEnd(reqId)
        print("PositionMultiEnd. RequestId:", reqId)
    # ! [positionmultiend]

    @iswrapper
    # ! [accountupdatemulti]
    def accountUpdateMulti(self, reqId: int, account: str, modelCode: str,
                           key: str, value: str, currency: str):
        super().accountUpdateMulti(reqId, account, modelCode, key, value,
                                   currency)
        print("AccountUpdateMulti. RequestId:", reqId, "Account:", account,
              "ModelCode:", modelCode, "Key:", key, "Value:", value,
              "Currency:", currency)
    # ! [accountupdatemulti]

    @iswrapper
    # ! [accountupdatemultiend]
    def accountUpdateMultiEnd(self, reqId: int):
        super().accountUpdateMultiEnd(reqId)
        print("AccountUpdateMultiEnd. RequestId:", reqId)
    # ! [accountupdatemultiend]

    @iswrapper
    # ! [familyCodes]
    def familyCodes(self, familyCodes: ListOfFamilyCode):
        super().familyCodes(familyCodes)
        print("Family Codes:")
        for familyCode in familyCodes:
            print("FamilyCode.", familyCode)
    # ! [familyCodes]

    @iswrapper
    # ! [pnl]
    def pnl(self, reqId: int, dailyPnL: float,
            unrealizedPnL: float, realizedPnL: float):
        super().pnl(reqId, dailyPnL, unrealizedPnL, realizedPnL)
        print("Daily PnL. ReqId:", reqId, "DailyPnL:", floatMaxString(dailyPnL),
              "UnrealizedPnL:", floatMaxString(unrealizedPnL), "RealizedPnL:", floatMaxString(realizedPnL))
    # ! [pnl]

    @iswrapper
    # ! [pnlsingle]
    def pnlSingle(self, reqId: int, pos: Decimal, dailyPnL: float,
                  unrealizedPnL: float, realizedPnL: float, value: float):
        super().pnlSingle(reqId, pos, dailyPnL, unrealizedPnL, realizedPnL, value)
        print("Daily PnL Single. ReqId:", reqId, "Position:", decimalMaxString(pos),
              "DailyPnL:", floatMaxString(dailyPnL), "UnrealizedPnL:", floatMaxString(unrealizedPnL),
              "RealizedPnL:", floatMaxString(realizedPnL), "Value:", floatMaxString(value))
    # ! [pnlsingle]

    @iswrapper
    # ! [marketdatatype]
    def marketDataType(self, reqId: TickerId, marketDataType: int):
        super().marketDataType(reqId, marketDataType)
        print("MarketDataType. ReqId:", reqId, "Type:", marketDataType)
    # ! [marketdatatype]

    @iswrapper
    # ! [tickprice]
    def tickPrice(self, reqId: TickerId, tickType: TickType, price: float,
                  attrib: TickAttrib):
        super().tickPrice(reqId, tickType, price, attrib)
        if tickType == TickTypeEnum.BID or tickType == TickTypeEnum.ASK:
            print("PreOpen:", attrib.preOpen)
        else:
            print()

        e = self.find_event_by_id(reqId)
        if not e == None:
            match tickType:
                case 1:
                    e.option_bid = price
                case 2:
                    e.option_ask = price
                case 4:
                    e.last = price

            if e.has_complete_data() and not e.option_req_cancelled:
                e.set()
                self.cancelMktData(reqId)
                e.option_req_cancelled = True
    # ! [tickprice]

    @iswrapper
    # ! [ticksize]
    def tickSize(self, reqId: TickerId, tickType: TickType, size: Decimal):
        super().tickSize(reqId, tickType, size)
    # ! [ticksize]

    @iswrapper
    # ! [tickgeneric]
    def tickGeneric(self, reqId: TickerId, tickType: TickType, value: float):
        super().tickGeneric(reqId, tickType, value)
        print("TickGeneric. TickerId:", reqId, "TickType:", tickType, "Value:", floatMaxString(value))
    # ! [tickgeneric]

    @iswrapper
    # ! [tickstring]
    def tickString(self, reqId: TickerId, tickType: TickType, value: str):
        super().tickString(reqId, tickType, value)
    # ! [tickstring]

    @iswrapper
    # ! [ticksnapshotend]
    def tickSnapshotEnd(self, reqId: int):
        super().tickSnapshotEnd(reqId)
        print("TickSnapshotEnd. TickerId:", reqId)
    # ! [ticksnapshotend]

    @iswrapper
    # ! [rerouteMktDataReq]
    def rerouteMktDataReq(self, reqId: int, conId: int, exchange: str):
        super().rerouteMktDataReq(reqId, conId, exchange)
        print("Re-route market data request. ReqId:", reqId, "ConId:", conId, "Exchange:", exchange)
    # ! [rerouteMktDataReq]

    @iswrapper
    # ! [marketRule]
    def marketRule(self, marketRuleId: int, priceIncrements: ListOfPriceIncrements):
        super().marketRule(marketRuleId, priceIncrements)
        print("Market Rule ID: ", marketRuleId)
        for priceIncrement in priceIncrements:
            print("Price Increment.", priceIncrement)
    # ! [marketRule]
        
    @iswrapper
    # ! [orderbound]
    def orderBound(self, orderId: int, apiClientId: int, apiOrderId: int):
        super().orderBound(orderId, apiClientId, apiOrderId)
        print("OrderBound.", "OrderId:", intMaxString(orderId), "ApiClientId:", intMaxString(apiClientId), "ApiOrderId:", intMaxString(apiOrderId))
    # ! [orderbound]

    @iswrapper
    # ! [tickbytickalllast]
    def tickByTickAllLast(self, reqId: int, tickType: int, time: int, price: float,
                          size: Decimal, tickAtrribLast: TickAttribLast, exchange: str,
                          specialConditions: str):
        super().tickByTickAllLast(reqId, tickType, time, price, size, tickAtrribLast,
                                  exchange, specialConditions)
        if tickType == 1:
            print("Last.", end='')
        else:
            print("AllLast.", end='')
        print(" ReqId:", reqId,
              "Time:", datetime.datetime.fromtimestamp(time).strftime("%Y%m%d-%H:%M:%S"),
              "Price:", floatMaxString(price), "Size:", decimalMaxString(size), "Exch:" , exchange,
              "Spec Cond:", specialConditions, "PastLimit:", tickAtrribLast.pastLimit, "Unreported:", tickAtrribLast.unreported)
    # ! [tickbytickalllast]

    @iswrapper
    # ! [tickbytickbidask]
    def tickByTickBidAsk(self, reqId: int, time: int, bidPrice: float, askPrice: float,
                         bidSize: Decimal, askSize: Decimal, tickAttribBidAsk: TickAttribBidAsk):
        super().tickByTickBidAsk(reqId, time, bidPrice, askPrice, bidSize,
                                 askSize, tickAttribBidAsk)
        print("BidAsk. ReqId:", reqId,
              "Time:", datetime.datetime.fromtimestamp(time).strftime("%Y%m%d-%H:%M:%S"),
              "BidPrice:", floatMaxString(bidPrice), "AskPrice:", floatMaxString(askPrice), "BidSize:", decimalMaxString(bidSize),
              "AskSize:", decimalMaxString(askSize), "BidPastLow:", tickAttribBidAsk.bidPastLow, "AskPastHigh:", tickAttribBidAsk.askPastHigh)
    # ! [tickbytickbidask]

    # ! [tickbytickmidpoint]
    @iswrapper
    def tickByTickMidPoint(self, reqId: int, time: int, midPoint: float):
        super().tickByTickMidPoint(reqId, time, midPoint)
        print("Midpoint. ReqId:", reqId,
              "Time:", datetime.datetime.fromtimestamp(time).strftime("%Y%m%d-%H:%M:%S"),
              "MidPoint:", floatMaxString(midPoint))
    # ! [tickbytickmidpoint]

    @iswrapper
    # ! [updatemktdepth]
    def updateMktDepth(self, reqId: TickerId, position: int, operation: int,
                       side: int, price: float, size: Decimal):
        super().updateMktDepth(reqId, position, operation, side, price, size)
        print("UpdateMarketDepth. ReqId:", reqId, "Position:", position, "Operation:",
              operation, "Side:", side, "Price:", floatMaxString(price), "Size:", decimalMaxString(size))
    # ! [updatemktdepth]

    @iswrapper
    # ! [updatemktdepthl2]
    def updateMktDepthL2(self, reqId: TickerId, position: int, marketMaker: str,
                         operation: int, side: int, price: float, size: Decimal, isSmartDepth: bool):
        super().updateMktDepthL2(reqId, position, marketMaker, operation, side,
                                 price, size, isSmartDepth)
        print("UpdateMarketDepthL2. ReqId:", reqId, "Position:", position, "MarketMaker:", marketMaker, "Operation:",
              operation, "Side:", side, "Price:", floatMaxString(price), "Size:", decimalMaxString(size), "isSmartDepth:", isSmartDepth)

    # ! [updatemktdepthl2]

    @iswrapper
    # ! [rerouteMktDepthReq]
    def rerouteMktDepthReq(self, reqId: int, conId: int, exchange: str):
        super().rerouteMktDataReq(reqId, conId, exchange)
        print("Re-route market depth request. ReqId:", reqId, "ConId:", conId, "Exchange:", exchange)
    # ! [rerouteMktDepthReq]

    @iswrapper
    # ! [realtimebar]
    def realtimeBar(self, reqId: TickerId, time:int, open_: float, high: float, low: float, close: float,
                        volume: Decimal, wap: Decimal, count: int):
        super().realtimeBar(reqId, time, open_, high, low, close, volume, wap, count)
        print("RealTimeBar. TickerId:", reqId, RealTimeBar(time, -1, open_, high, low, close, volume, wap, count))
    # ! [realtimebar]

    @iswrapper
    # ! [headTimestamp]
    def headTimestamp(self, reqId:int, headTimestamp:str):
        print("HeadTimestamp. ReqId:", reqId, "HeadTimeStamp:", headTimestamp)
    # ! [headTimestamp]

    @iswrapper
    # ! [histogramData]
    def histogramData(self, reqId:int, items:HistogramDataList):
        print("HistogramData. ReqId:", reqId, "HistogramDataList:", "[%s]" % "; ".join(map(str, items)))
    # ! [histogramData]

    @iswrapper
    # ! [historicaldata]
    def historicalData(self, reqId:int, bar: BarData):
        print("HistoricalData. ReqId:", reqId, "BarData.", bar)
    # ! [historicaldata]

    @iswrapper
    # ! [historicaldataend]
    def historicalDataEnd(self, reqId: int, start: str, end: str):
        super().historicalDataEnd(reqId, start, end)
        print("HistoricalDataEnd. ReqId:", reqId, "from", start, "to", end)
    # ! [historicaldataend]

    @iswrapper
    # ! [historicalDataUpdate]
    def historicalDataUpdate(self, reqId: int, bar: BarData):
        print("HistoricalDataUpdate. ReqId:", reqId, "BarData.", bar)
    # ! [historicalDataUpdate]

    @iswrapper
    # ! [historicalticks]
    def historicalTicks(self, reqId: int, ticks: ListOfHistoricalTick, done: bool):
        for tick in ticks:
            print("HistoricalTick. ReqId:", reqId, tick)
    # ! [historicalticks]

    @iswrapper
    # ! [historicalticksbidask]
    def historicalTicksBidAsk(self, reqId: int, ticks: ListOfHistoricalTickBidAsk,
                              done: bool):
        for tick in ticks:
            print("HistoricalTickBidAsk. ReqId:", reqId, tick)
    # ! [historicalticksbidask]

    @iswrapper
    # ! [historicaltickslast]
    def historicalTicksLast(self, reqId: int, ticks: ListOfHistoricalTickLast,
                            done: bool):
        for tick in ticks:
            print("HistoricalTickLast. ReqId:", reqId, tick)
    # ! [historicaltickslast]

    @iswrapper
    # ! [securityDefinitionOptionParameter]
    def securityDefinitionOptionParameter(self, reqId: int, exchange: str,
                                          underlyingConId: int, tradingClass: str, multiplier: str,
                                          expirations: SetOfString, strikes: SetOfFloat):
        super().securityDefinitionOptionParameter(reqId, exchange,
                                                  underlyingConId, tradingClass, multiplier, expirations, strikes)
        print("SecurityDefinitionOptionParameter.",
              "ReqId:", reqId, "Exchange:", exchange, "Underlying conId:", intMaxString(underlyingConId), "TradingClass:", tradingClass, "Multiplier:", multiplier,
              "Expirations:", expirations, "Strikes:", str(strikes))
        event = self.find_event_by_id(reqId)
        #use SMART only
        if not event == None and exchange == "SMART":
            #AM settlement is not supported for now
            #for expiry dates with multiple trading class, note that weeekly trading class is always longer
            #e.g. SPX for AM and SPXW for PM

            if event.target_expiration != None and event.target_expiration in expirations:
                if event.option_trading_class == None or len(event.option_trading_class) < len(tradingClass):
                    #first trading class or PM settlement class found
                    event.option_strikes = strikes
                    event.option_expirations = expirations
                    event.option_trading_class = tradingClass
                
    # ! [securityDefinitionOptionParameter]

    @iswrapper
    # ! [securityDefinitionOptionParameterEnd]
    def securityDefinitionOptionParameterEnd(self, reqId: int):
        super().securityDefinitionOptionParameterEnd(reqId)
        print("SecurityDefinitionOptionParameterEnd. ReqId:", reqId)
        self.notify_event(reqId)
    # ! [securityDefinitionOptionParameterEnd]

    def cancel_excess_option_request(self, strike: float, side: str):
        """
        Cancell requests farther than the specified strike, since their delta is too low
        """
        logging.warning("Cancelling option data requests...")
        rqs = []
        for k in self.request_events.keys():
            r = self.request_events[k]
            if r.is_set() or r.contract == None:
                #unrelated
                continue

            if (side == r.contract.right == "P" and r.contract.strike) < strike or (side == r.contract.right == "C" and r.contract.strike > strike):
                rqs.append(r)

        counter = 0
        for r in rqs:
            logging.warning(f"Cancelling request for strike {r.contract.strike}{r.contract.right}")
            r.set()
            self.cancelMktData(r.order_id)
            r.option_req_cancelled = True
            counter += 1
            if counter % 20 == 0:
                time.sleep(1)



    @iswrapper
    # ! [tickoptioncomputation]
    def tickOptionComputation(self, reqId: TickerId, tickType: TickType, tickAttrib: int,
                              impliedVol: float, delta: float, optPrice: float, pvDividend: float,
                              gamma: float, vega: float, theta: float, undPrice: float):
        super().tickOptionComputation(reqId, tickType, tickAttrib, impliedVol, delta,
                                      optPrice, pvDividend, gamma, vega, theta, undPrice)
        # print("TickOptionComputation. TickerId:", reqId, "TickType:", tickType,
        #       "TickAttrib:",tickAttrib,
        #       "ImpliedVolatility:", impliedVol, "Delta:", delta, "OptionPrice:",
        #       optPrice, "pvDividend:", pvDividend, "Gamma: ", gamma, "Vega:", vega,
        #       "Theta:", theta, "UnderlyingPrice:", undPrice)

        e = self.find_event_by_id(reqId)
        if not e == None and delta != None:
            match tickType:
                case 10:
                    e.option_delta_bid = round(Decimal(delta),4)
                    e.check_delta()
                case 11:
                    e.option_delta_ask = round(Decimal(delta),4)
                    e.check_delta()
                case 12:
                    e.option_delta_last = round(Decimal(delta),4)
                    e.check_delta()
                case 13:
                    e.option_delta_model = round(Decimal(delta),4)
                    e.check_delta()

            if e.has_complete_data() and not e.option_req_cancelled:
                e.set()
                self.cancelMktData(e.order_id)
                e.option_req_cancelled = True

                cmp = e.compare_delta(0.01)
                if cmp != None and cmp > 0:
                    #delta too small, cancell other requests 
                    self.cancel_excess_option_request(e.contract.strike, e.contract.right)

    # ! [tickoptioncomputation]

    @iswrapper
    # ! [contractdetails]
    def contractDetails(self, reqId: int, contractDetails: ContractDetails):
        super().contractDetails(reqId, contractDetails)
        printinstance(contractDetails)

        if reqId in self.request_events:
            event = self.request_events [reqId]
            
            event.contract_details.append(contractDetails)
    # ! [contractdetails]

    @iswrapper
    # ! [contractdetailsend]
    def contractDetailsEnd(self, reqId: int):
        super().contractDetailsEnd(reqId)
        print("ContractDetailsEnd. ReqId:", reqId)

        if reqId in self.request_events:
            self.request_events[reqId].set()
    # ! [contractdetailsend]

    @iswrapper
    # ! [smartcomponents]
    def smartComponents(self, reqId:int, smartComponentMap:SmartComponentMap):
        super().smartComponents(reqId, smartComponentMap)
        print("SmartComponents:")
        for smartComponent in smartComponentMap:
            print("SmartComponent.", smartComponent)
    # ! [smartcomponents]

    @iswrapper
    # ! [tickReqParams]
    def tickReqParams(self, tickerId:int, minTick:float,
                      bboExchange:str, snapshotPermissions:int):
        super().tickReqParams(tickerId, minTick, bboExchange, snapshotPermissions)
        print("TickReqParams. TickerId:", tickerId, "MinTick:", floatMaxString(minTick),
              "BboExchange:", bboExchange, "SnapshotPermissions:", intMaxString(snapshotPermissions))
    # ! [tickReqParams]

    @iswrapper
    # ! [mktDepthExchanges]
    def mktDepthExchanges(self, depthMktDataDescriptions:ListOfDepthExchanges):
        super().mktDepthExchanges(depthMktDataDescriptions)
        print("MktDepthExchanges:")
        for desc in depthMktDataDescriptions:
            print("DepthMktDataDescription.", desc)
    # ! [mktDepthExchanges]

    @printWhenExecuting
    def miscelaneousOperations(self):
        # Request TWS' current time
        self.reqCurrentTime()
        # Setting TWS logging level
        self.setServerLogLevel(1)

    def get_daily_trade_contract(self):
        """
        Get the target contract for trading. 
        By default it will be ES futures if not specified by CMD line or the run() function.
        Otherwise it will return the contract found during the contract search at the start of the program.
        """
        if self.contract is not None:
            return self.contract

        contract = Contract()
        contract.symbol = "ES"
        contract.secType = "FUT"
        contract.exchange = "CME"
        contract.currency = "USD"
        contract.lastTradeDateOrContractMonth = "202303"

        self.contract = contract
        return contract

    @iswrapper
    # ! [execdetails]
    def execDetails(self, reqId: int, contract: Contract, execution: Execution):
        super().execDetails(reqId, contract, execution)
        print("ExecDetails. ReqId:", reqId, "Symbol:", contract.symbol, "SecType:", contract.secType, "Currency:", contract.currency, execution)
    # ! [execdetails]

    @iswrapper
    # ! [execdetailsend]
    def execDetailsEnd(self, reqId: int):
        super().execDetailsEnd(reqId)
        print("ExecDetailsEnd. ReqId:", reqId)
    # ! [execdetailsend]

    @iswrapper
    # ! [commissionreport]
    def commissionReport(self, commissionReport: CommissionReport):
        super().commissionReport(commissionReport)
        print("CommissionReport.", commissionReport)
    # ! [commissionreport]

    @iswrapper
    # ! [currenttime]
    def currentTime(self, time:int):
        super().currentTime(time)
        print("CurrentTime:", datetime.datetime.fromtimestamp(time).strftime("%Y%m%d-%H:%M:%S"))
    # ! [currenttime]

    @iswrapper
    # ! [completedorder]
    def completedOrder(self, contract: Contract, order: Order,
                  orderState: OrderState):
        super().completedOrder(contract, order, orderState)
        print("CompletedOrder. PermId:", intMaxString(order.permId), "ParentPermId:", longMaxString(order.parentPermId), "Account:", order.account, 
              "Symbol:", contract.symbol, "SecType:", contract.secType, "Exchange:", contract.exchange, 
              "Action:", order.action, "OrderType:", order.orderType, "TotalQty:", decimalMaxString(order.totalQuantity), 
              "CashQty:", floatMaxString(order.cashQty), "FilledQty:", decimalMaxString(order.filledQuantity), 
              "LmtPrice:", floatMaxString(order.lmtPrice), "AuxPrice:", floatMaxString(order.auxPrice), "Status:", orderState.status,
              "Completed time:", orderState.completedTime, "Completed Status:" + orderState.completedStatus,
              "MinTradeQty:", intMaxString(order.minTradeQty), "MinCompeteSize:", intMaxString(order.minCompeteSize),
              "competeAgainstBestOffset:", "UpToMid" if order.competeAgainstBestOffset == COMPETE_AGAINST_BEST_OFFSET_UP_TO_MID else floatMaxString(order.competeAgainstBestOffset),
              "MidOffsetAtWhole:", floatMaxString(order.midOffsetAtWhole),"MidOffsetAtHalf:" ,floatMaxString(order.midOffsetAtHalf))
    # ! [completedorder]

    @iswrapper
    # ! [completedordersend]
    def completedOrdersEnd(self):
        super().completedOrdersEnd()
        print("CompletedOrdersEnd")
    # ! [completedordersend]

    @iswrapper
    # ! [userinfo]
    def userInfo(self, reqId: int, whiteBrandingId: str):
        super().userInfo(reqId, whiteBrandingId)
        print("UserInfo.", "ReqId:", reqId, "WhiteBrandingId:", whiteBrandingId)
    # ! [userinfo]

def get_wsl_host_ip():
    """
    Get the IP of the host for connecting to IB Gateway

    File to look for is /etc/resolv.conf in WSL. Nameserver will be the IP of the host.

    Returns:
        The IP found in the aforementioned file. If the system is not WSL then localhost will be returned.
    """
    if not 'microsoft-standard' in uname().release: #not WSL
        return "localhost"

    with open("/etc/resolv.conf", "r") as f:
        for l in f.readlines():
            if l.find("nameserver") >= 0:
                ip = l[11:].rstrip()
                print(f"WSL HOST IP: {ip}") 
                return ip
    return ""

def run(
    quantity: int, 
    check_positions_only: bool, 
    port: int, dry_run: bool, 
    ticker: str = "", 
    future: str = "", 
    mode: int = 1, 
    short_leg_delta: float = 0, 
    long_leg_delta: float = 0, 
    stop_loss_percentage: float = 0, 
    dte: int = 0,
    auto_retry_fill_interval: int = 0,
    auto_retry_price_decrement: float = 0.05,
    profit_taking_percentage: float = 0.0
    ):
    stocks = []
    futures = []

    if not ticker == "" and not future == "":
        logging.error("Ticker and future cannot be set at the same time!")
        return

    if not ticker == "":
        stocks.append((ticker, quantity))
    elif not future == "":
        futures.append((future, quantity))

    return run_trading_program(
        check_positions_only, 
        port, 
        dry_run, 
        stocks, 
        futures, 
        mode, 
        short_leg_delta, 
        long_leg_delta, 
        stop_loss_percentage, 
        dte,
        auto_retry_fill_interval,
        auto_retry_price_decrement,
        profit_taking_percentage
        )

def run_trading_program(
    check_positions_only: bool, 
    port: int, 
    dry_run: bool, 
    tickers: list[tuple[str,int]] = None, 
    futures: list[tuple[str,int]] = None, 
    mode: int = 1, 
    short_leg_delta: float = 0, 
    long_leg_delta: float = 0, 
    stop_loss_percentage: float = 0, 
    dte: int = 0,
    auto_retry_fill_interval: int = 0,
    auto_retry_price_decrement: float = 0.05,
    profit_taking_percentage: float = 0.0
    ):
    logging.debug("now is %s", datetime.datetime.now())

    # enable logging when member vars are assigned
    from ibapi import utils
    Order.__setattr__ = utils.setattr_log
    Contract.__setattr__ = utils.setattr_log
    DeltaNeutralContract.__setattr__ = utils.setattr_log
    TagValue.__setattr__ = utils.setattr_log
    TimeCondition.__setattr__ = utils.setattr_log
    ExecutionCondition.__setattr__ = utils.setattr_log
    MarginCondition.__setattr__ = utils.setattr_log
    PriceCondition.__setattr__ = utils.setattr_log
    PercentChangeCondition.__setattr__ = utils.setattr_log
    VolumeCondition.__setattr__ = utils.setattr_log

    try:
        app = TradeApp()
        #if args.global_cancel:
        #    app.globalCancelOnly = True

        app.tickers = tickers
        app.futures = futures

        app.check_positions_only = check_positions_only
        app.dry_run = dry_run
        app.options_trading_mode = mode
        app.short_leg_delta = short_leg_delta
        app.long_leg_delta = long_leg_delta
        app.stop_loss_percentage = stop_loss_percentage
        app.dte = dte
        app.auto_retry_fill_interval = auto_retry_fill_interval
        app.auto_retry_price_decrement = auto_retry_price_decrement
        app.profit_taking_percentage = profit_taking_percentage

        #ip = get_wsl_host_ip()
        ip = "localhost"

        # ! [connect]
        app.connect(ip, port, clientId=0)
        # ! [connect]
        print("serverVersion:%s connectionTime:%s" % (app.serverVersion(),
                                                      app.twsConnectionTime()))

        # ! [clientrun]
        app.run()
        # ! [clientrun]
        
    except BaseException as ex:
        print(ex)
        raise
    finally:
        app.dumpTestCoverageSituation()
        app.dumpReqAnsErrSituation()

def main():
    cmdLineParser = argparse.ArgumentParser("Trade 0DTE (or longer duration) options.")
    cmdLineParser.add_argument("-p", "--port", action="store", type=int,
                               dest="port", default=os.environ.get("PORT", 7496), help="The TCP port to use")
    cmdLineParser.add_argument("-q", "--quantity", type=int, dest="quantity", default=os.environ.get("QUANTITY", 1), help="The amount of stock or futures option combos to trade.")
    cmdLineParser.add_argument("-c", "--check_only", type=bool, dest="check_only", default=os.environ.get("CHECK_ONLY", False), help="Check account position only.")
    cmdLineParser.add_argument("-d", "--dry_run", type=bool, dest="dry_run", default=os.environ.get("DRY_RUN", "false").lower() == "true", help="Dry run.")
    cmdLineParser.add_argument("-m", "--mode", type=int, dest="mode", choices=[1, 2, 3, 4, 5, 6, 7, 8], default=os.environ.get("MODE", 1), help="Mode: 1 for Bull / Bear Put, 2 for Bear / Bull Call, 3 for Iron Condor / Iron Butterfly, 4 for Butterfly, 5 - 8 for Short Put/Short Call/Long Put/Long Call respectively.")
    cmdLineParser.add_argument("-s", "--short_leg_delta", type=float, dest="short_leg_delta", default=os.environ.get("SHORT_LEG_DELTA", 0.16), help="Delta of the short leg. Should be a float in range of [0,1].")
    cmdLineParser.add_argument("-l", "--long_leg_delta", type=float, dest="long_leg_delta", default=os.environ.get("LONG_LEG_DELTA", 0.1), help="Delta of the long leg. Should be a float in range of [0,1].")
    cmdLineParser.add_argument("-x", "--stop_loss_percentage", type=float, dest="stop_loss_percentage", default=os.environ.get("STOP_LOSS_PERCENTAGE", 3.0), help="Percentage of stop loss as the premium received. e.g. 3.0 for setting stop loss at 300 percent of premium received.")
    cmdLineParser.add_argument("-e", "--day_to_expiry", type=int, default=os.environ.get("DAY_TO_EXPIRY", 0), dest="dte", help="Day to expiry for the target option contract(s).")
    cmdLineParser.add_argument("-ai", "--auto_retry_fill_interval", type=int, default=os.environ.get("AUTO_RETRY_INTERVAL", 10), dest="auto_retry_fill_interval", help="If set and is larger than zero, order will be resubmitted by the interval specified. In each interal, order price will be decremented by the amount specified by param -ap.")
    cmdLineParser.add_argument("-ap", "--auto_retry_price_decrement", type=float, default=os.environ.get("AUTO_RETRY_PRICE_DECREMENT", 0.5), dest="auto_retry_price_decrement", help="Decrements price towards 0 by the value specified each time the order is resubmitted.")
    cmdLineParser.add_argument("-pt", "--profit_taking_percentage", type=float, default=os.environ.get("PROFIT_TAKING_PERCENTAGE", 0.0), dest="profit_taking_percentage", help="Percentage of premium for the profit taking order. e.g. 0.5 to take profit at 50% of credit received. If unset or set to 0, will not send profit taking orders to IBKR.")
    group = cmdLineParser.add_mutually_exclusive_group()
    group.add_argument("-t", "--ticker", type=str, dest="ticker", required=False, default=os.environ.get("TICKER", ""), help="Ticker to trade. Can be US stocks or indexes with options only. Cannot be used with -f flag at the same time.")
    group.add_argument("-f", "--future", type=str, dest="future", required=False, default=os.environ.get("FUTURE", ""), help="[NOT IMPLEMENTED YET] Futures contract to trade. Cannot be used with -t flag at the same time.")
    args = cmdLineParser.parse_args()
    print("Using args", args)
    logging.debug("Using args %s", args)
    # print(args)

    run(args.quantity, 
    args.check_only, 
    args.port, 
    args.dry_run, 
    args.ticker, 
    args.future, 
    args.mode, 
    args.short_leg_delta, 
    args.long_leg_delta, 
    args.stop_loss_percentage, 
    args.dte,
    args.auto_retry_fill_interval,
    args.auto_retry_price_decrement,
    args.profit_taking_percentage)

def setup_logging():
    import os
    import logging as log
    from logging.handlers import TimedRotatingFileHandler
    #import google.cloud.logging

    LOGGING_FORMAT = "%(asctime)s %(threadName)s:%(module)s:%(funcName)s %(levelname)s %(message)s"

    #logging
    if not os.path.exists("log"):
        os.mkdir("log")
    LOG_LEVEL = log.INFO
    #log.basicConfig(filename=f'log/auto_trade-%Y%m%d.log', format=LOGGING_FORMAT, level=log.INFO)
    formatter = log.Formatter(LOGGING_FORMAT, style='%')
    logger = log.getLogger()
    logger.setLevel(LOG_LEVEL)
    console = log.StreamHandler()
    console.setFormatter(formatter)
    #file = TimedRotatingFileHandler(filename='log/auto_trade.log', when="D", interval=1)
    #file.setFormatter(formatter)
    logger.addHandler(console)
    #logger.addHandler(file)

    #Google Cloud
    #client = google.cloud.logging.Client()
    #client.setup_logging()


if __name__ == "__main__":
    setup_logging()
    main()
