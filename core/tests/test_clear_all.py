"""Clearing data works whatever has been recorded (every kind of row that points at transactions / agents)."""
import uuid

from django.test import TestCase

from core.models import Account, Counterparty, Transaction
from core.services import admin as adm
from core.services import agents as ag
from core.services import operations as ops
from core.services import shop_shifts as ss
from core.services import shops as sh

from .helpers import D, OrgMixin
from .test_web import PW

SCOPES = ["user", "agents", "shops", "transactions", "pos_list", "agent_list", "everything"]


class ClearEverything(OrgMixin, TestCase):
    def setUp(self):
        from core.models import User

        self.make_org()
        self.fund_all()
        shop, m = self.o["shop"], self.o["shop_mgr"]
        for k in (Account.Kind.CASH_DRAWER, Account.Kind.UVE, Account.Kind.ER):
            self.fund(shop.account(k), 300000)
        ss.open_shift(m, cash=self.bal(shop.account(Account.Kind.CASH_DRAWER)),
                      uve=self.bal(shop.account(Account.Kind.UVE)), er=self.bal(shop.account(Account.Kind.ER)))
        p = sh.receive_agent_payback(m, agent=self.a1, cash=D(5000))
        sh.forward_payback(m, p)  # PaybackForward
        sh.exchange(m, partner="DESK", desk=self.c_f1.subzone, give="UVE", amount=D(1000))
        sh.destockage(m, agent=self.a1, amount=D(2000), pay="CREDIT")
        ops.record_entry(self.c_f1, source="SHOP", shop=shop, product="CASH", amount=D(1000))
        ops.stock_exchange(self.c_f1, partner="SHOP", partner_shop=shop, give="CASH", amount=D(1000))
        au = User.objects.get(counterparty=self.a1)
        ag.declare(au, recv_uve=D(1000))
        ag.declare(au, kind="OPENING", recv_cash=D(1000))
        ag.request_payback(au, shop=shop, cash=D(1000))
        ag.record_capital(self.admin, agent=self.a1, movement="GIVE", amount=D(50000))
        self.agent_user = au
        # an older entry answered "no payback" (TransferNoPayback points at it)
        from core.models import TransferNoPayback

        old = ops.record_entry(self.c_f2, source="DESK", desk=self.c_f1.subzone, product="CASH", amount=D(500))
        TransferNoPayback.objects.create(transfer=old, decided_by=self.c_f2)
        own = Counterparty.objects.create(kind="AGENT", business_sim="655001122", name="Supervisor SIM")
        self.mk = User.objects.create_user("mkclear", password=PW, role=User.Role.MARKET,
                                           subzone=self.p1.microzone.subzone, counterparty=own)

    def _run(self, scopes, **kw):
        adm.clear_many(self.admin, scopes=scopes, password=PW, confirm_text="DELETE", **kw)

    def test_each_scope(self):
        for s in SCOPES:
            with self.subTest(scope=s):
                kw = {"agent": self.a1} if s == "agents" else {"target_user": self.c_f1} if s == "user" else {}
                sid = __import__("django.db", fromlist=["transaction"]).transaction.savepoint()
                try:
                    self._run([s], **kw)
                finally:
                    __import__("django.db", fromlist=["transaction"]).transaction.savepoint_rollback(sid)

    def test_several_at_once_and_web(self):
        c = self.client_class()
        self.assertTrue(c.login(username=self.admin.username, password=PW))
        r = c.post("/administration/clear/", {"scope": ["shops", "transactions", "pos_list", "agent_list"],
                                              "password": PW, "confirm_text": "DELETE"})
        self.assertIn(r.status_code, (200, 302))
        self.assertFalse(Transaction.objects.exists())
        self.assertFalse(Counterparty.objects.filter(kind__in=["POS", "AGENT"]).exists())
        self.mk.refresh_from_db()
        self.assertIsNone(self.mk.counterparty_id)  # the supervisor keeps his login
        r = c.post("/administration/clear/", {"scope": ["everything"], "password": PW, "confirm_text": "DELETE"})
        self.assertIn(r.status_code, (200, 302))
