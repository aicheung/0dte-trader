from threading import Event
from decimal import Decimal

class IBKREvent(Event):
    def __init__(self, order_id, contract = None) -> None:
        self.order_id = order_id
        self.contract = contract
        self.target_expiration = None
        self.option_strikes = None
        self.option_expirations = None
        self.option_trading_class = None
        self.option_bid = None
        self.option_ask = None
        self.option_delta = None
        self.option_delta_bid = None
        self.option_delta_ask = None
        self.option_delta_last = None
        self.option_delta_model = None
        self.option_req_cancelled = False
        
        self.get_prices = True

        self.last = None #for underlying
        super().__init__()

    def compare_delta(self, d: float | Decimal):
        d = Decimal(d)
        cur_del = self.option_delta

        if cur_del == None:
            return None
            
        cur_del = abs(cur_del)
        return 1 if d > cur_del else 0 if d == cur_del else -1

    def check_delta(self):
        if None not in [self.option_delta_bid, self.option_delta_ask, self.option_delta_last, self.option_delta_model]:
            self.option_delta = max([self.option_delta_bid, self.option_delta_ask, self.option_delta_last, self.option_delta_model])
        elif None not in [self.option_delta_bid, self.option_delta_ask, self.option_delta_model]:
            self.option_delta = max([self.option_delta_bid, self.option_delta_ask, self.option_delta_model])

    def has_complete_data(self):
        price_done = (not self.get_prices) or (self.option_bid != None and self.option_ask != None)
        if self.contract != None:
            match self.contract.secType:
                case 'OPT':
                    return price_done and self.option_delta != None
                case 'BAG':
                    return price_done #no need delta for combo
                case 'IND':
                    return self.last != None
                case 'STK':
                    return self.last != None
        
        return True #non data request?
