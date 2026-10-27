import uuid
from decimal import Decimal

from core.models import Account
from core.sample import build_sample_org
from core.services import operations as ops
from core.services.ledger import balance

D = Decimal


def key():
    return uuid.uuid4()


class OrgMixin:
    def make_org(self):
        o = build_sample_org()
        self.o = o
        for k, v in o.items():
            setattr(self, k, v)
        self.a1, self.a2, self.a3 = o["agents"]["a1"], o["agents"]["a2"], o["agents"]["a3"]
        self.p1, self.p2 = o["pos"]["p1"], o["pos"]["p2"]
        self.d_f1, self.d_f2, self.d_me = o["drawers"]["f1"], o["drawers"]["f2"], o["drawers"]["me"]
        return o

    def fund(self, account, amount):
        return ops.record_opening_balance(self.admin, account=account, amount=D(amount), description="test funding")

    def fund_all(self):
        for a in [self.d_f1, self.d_f2, self.d_me, self.uve_f1, self.uve_f2, self.uve_me, self.er_f1, self.er_me]:
            self.fund(a, 1_000_000)

    def bal(self, account):
        return balance(account)

    def cbal(self, cp):
        return balance(cp.account)

    def sysbal(self, kind):
        return balance(Account.system(kind))
